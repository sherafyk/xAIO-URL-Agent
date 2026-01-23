#!/usr/bin/env python3
"""call_openai_claims.py

Pipeline stage 3b (AI claims):
- Reads a *.ai_input.json (WITH extracted_text_full)
- Reads the matching *.meta_parsed.json
- Calls OpenAI Structured Outputs to extract claims ONLY from extracted_text_full
- Writes *.claims_parsed.json (and optionally *.claims_response_raw.json)

Key design goals:
- Strict, schema validated JSON output
- SCF `claim_type` select values enforced as JSON-schema enums
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError
from pydantic import BaseModel, ConfigDict, Field
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from env_bootstrap import load_repo_env
from logging_utils import elapsed_ms, log_event, setup_logging
from openai_compat import structured_parse

load_repo_env()
logger = setup_logging("call_openai_claims")


# ---------------------------
# IO helpers
# ---------------------------

def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------
# SCF export -> enum helpers
# ---------------------------

def find_select_choices(scf_export: Any, field_name: str) -> Dict[str, str]:
    if not isinstance(scf_export, list):
        return {}
    for group in scf_export:
        fields = group.get("fields", []) if isinstance(group, dict) else []
        for f in fields:
            if not isinstance(f, dict):
                continue
            if f.get("name") == field_name and f.get("type") == "select":
                return f.get("choices", {}) or {}
            if f.get("type") in ("repeater", "group"):
                for sf in f.get("sub_fields", []) or []:
                    if isinstance(sf, dict) and sf.get("name") == field_name and sf.get("type") == "select":
                        return sf.get("choices", {}) or {}
    return {}


def scf_claim_type_choices(scf_export_path: Path) -> List[str]:
    scf = load_json(scf_export_path)
    keys = list((find_select_choices(scf, "claim_type") or {}).keys())
    return sorted([k for k in keys if isinstance(k, str) and k.strip()])


def _enum_member_name(value: str) -> str:
    s = (value or "").strip()
    s = re.sub(r"[^0-9a-zA-Z_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    s = s.upper() or "VALUE"
    if s[0].isdigit():
        s = f"V_{s}"
    return s


def make_str_enum(enum_name: str, values: List[str]) -> Optional[Type[Enum]]:
    vals = [v.strip() for v in values if isinstance(v, str) and v.strip()]
    if not vals:
        return None

    mapping: Dict[str, str] = {}
    used: set[str] = set()
    for v in vals:
        key = _enum_member_name(v)
        base = key
        i = 1
        while key in used:
            i += 1
            key = f"{base}_{i}"
        used.add(key)
        mapping[key] = v

    return Enum(enum_name, mapping)


# ---------------------------
# Schema
# ---------------------------

def normalize_claim_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def build_model(claim_types: List[str]) -> Type[BaseModel]:
    ClaimTypeEnum = make_str_enum("ClaimType", claim_types)
    ClaimTypeT = ClaimTypeEnum or str

    class Claim(BaseModel):
        model_config = ConfigDict(use_enum_values=True)

        claim_text: str = Field(..., description="One atomic, checkable statement from the content.")
        claim_type: ClaimTypeT = Field(..., description="SCF claim_type select value.")

    class ClaimsParsed(BaseModel):
        model_config = ConfigDict(use_enum_values=True)

        claims: List[Claim] = Field(default_factory=list)

    return ClaimsParsed


SYSTEM_PROMPT = """You are xAIO Claims Extractor.

Return ONLY valid JSON matching the provided schema.
Do not include markdown, commentary, or extra keys.

Rules:
- Extract claims ONLY from extracted_text_full.
- One claim per atomic, checkable statement.
- Do NOT fact-check. Do NOT output verdict/confidence/severity/source/notes.
- Output ONLY claim_text and claim_type per claim.
"""


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((RateLimitError, APITimeoutError, APIConnectionError, APIError)),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def call_openai_structured(
    model: str,
    schema: Type[BaseModel],
    user_input: Dict[str, Any],
    reasoning_effort: Optional[str],
) -> Tuple[Optional[BaseModel], Dict[str, Any]]:
    client = OpenAI()
    return structured_parse(
        client,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_content=json.dumps(user_input, ensure_ascii=False),
        schema=schema,
        reasoning_effort=reasoning_effort,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ai_input_json")
    ap.add_argument("meta_parsed_json")
    ap.add_argument("--scf-export", default="config/scf-export-content.json")
    ap.add_argument("--outdir", default="./out_claims")
    ap.add_argument("--write-raw", action="store_true")
    ap.add_argument("--model", default="gpt-5-nano")
    ap.add_argument("--reasoning-effort", default="minimal")
    args = ap.parse_args()

    ai_path = Path(args.ai_input_json).expanduser().resolve()
    meta_path = Path(args.meta_parsed_json).expanduser().resolve()
    scf_path = Path(args.scf_export).expanduser().resolve()
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    item_id = ai_path.stem.replace(".ai_input", "")
    start_time = time.monotonic()
    log_event(logger, stage="claims_start", item_id=item_id, message="claims parse starting")

    try:
        ai_input = load_json(ai_path)
        _meta = load_json(meta_path)  # not currently used, but kept for future cross-field validation

        canon = (
            ((ai_input.get("url") or {}).get("clean") or {}).get("canonical")
            or (ai_input.get("url") or {}).get("final")
            or (ai_input.get("url") or {}).get("original")
        )

        user_input = {
            "canonical_url": canon,
            "meta": ai_input.get("meta", {}),
            "content": {
                "extracted_text_full": (ai_input.get("content") or {}).get("extracted_text_full", "")
            },
        }

        claim_types = scf_claim_type_choices(scf_path)
        if not claim_types:
            # Failsafe: keep pipeline moving even if SCF export is missing claim_type.
            # (Still schema-validated, just not enum-restricted.)
            logger.warning("SCF claim_type choices empty; claim_type will not be enum-restricted.")

        Schema = build_model(claim_types)

        parsed, raw = call_openai_structured(args.model, Schema, user_input, args.reasoning_effort)
        if parsed is None:
            raw_path = outdir / (item_id + ".claims_response_raw.json")
            raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            raise RuntimeError(f"Parse failure/refusal. Raw: {raw_path}")

        out = parsed.model_dump()

        # Normalize + dedupe claim text
        cleaned: List[Dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for c in out.get("claims", []) or []:
            ct = normalize_claim_text(c.get("claim_text", ""))
            ctype = (c.get("claim_type") or "").strip()
            if not ct:
                continue
            key = (ct, ctype)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append({"claim_text": ct, "claim_type": ctype})

        out = {"claims": cleaned}

        out_path = outdir / f"{item_id}.claims_parsed.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.write_raw:
            raw_path = outdir / f"{item_id}.claims_response_raw.json"
            raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

        log_event(
            logger,
            stage="claims_done",
            item_id=item_id,
            elapsed_ms_value=elapsed_ms(start_time),
            message=f"wrote={out_path} claims={len(out.get('claims', []))}",
        )
        return 0

    except Exception as exc:
        log_event(
            logger,
            stage="claims_failed",
            item_id=item_id,
            elapsed_ms_value=elapsed_ms(start_time),
            message=f"{type(exc).__name__}: {exc}",
            level=logging.ERROR,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
