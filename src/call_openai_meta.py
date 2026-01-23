#!/usr/bin/env python3
"""call_openai_meta.py

Pipeline stage 3a (AI metadata):
- Reads a *.meta_input.json (created from *.ai_input.json but WITHOUT full body text)
- Calls OpenAI Structured Outputs to extract META fields
- Writes *.meta_parsed.json (and optionally *.meta_response_raw.json)

Key design goals:
- **Strict, schema-validated JSON output** using Structured Outputs
- **No "mystery" config**: this script loads env vars from `.runtime/.env` via env_bootstrap
- **Enum enforcement**: SCF select fields are enforced as JSON-schema enums
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type

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
logger = setup_logging("call_openai_meta")


# ---------------------------
# IO helpers
# ---------------------------

def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------
# SCF export -> enum helpers
# ---------------------------

def find_select_choices(scf_export: Any, field_name: str) -> Dict[str, str]:
    """Return the SCF select "choices" dict for a field name, if present."""
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
                    if (
                        isinstance(sf, dict)
                        and sf.get("name") == field_name
                        and sf.get("type") == "select"
                    ):
                        return sf.get("choices", {}) or {}

    return {}


def scf_enums_from_export(scf_export_path: Path) -> Dict[str, List[str]]:
    scf = load_json(scf_export_path)

    def keys(name: str) -> List[str]:
        raw = list((find_select_choices(scf, name) or {}).keys())
        vals = [x.strip() for x in raw if isinstance(x, str) and x.strip()]
        return sorted(vals)

    return {
        "content_mode": keys("content_mode"),
        "language": keys("language"),
        "workflow_status": keys("workflow_status"),
        "intake_kind": keys("intake_kind"),
    }


def _enum_member_name(value: str) -> str:
    name = re.sub(r"[^0-9A-Za-z_]+", "_", (value or "").strip()).upper()
    name = name.strip("_")
    if not name:
        name = "VALUE"
    if name[0].isdigit():
        name = f"V_{name}"
    return name


def make_str_enum(enum_name: str, values: Sequence[str]) -> Optional[Type[Enum]]:
    """Create a string Enum whose values are the original SCF keys.

    Returns None if `values` is empty.
    """
    vals = [v for v in values if isinstance(v, str) and v.strip()]
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


def build_model(enums: Dict[str, List[str]]) -> Type[BaseModel]:
    ContentModeEnum = make_str_enum("ContentMode", enums.get("content_mode", []))
    LanguageEnum = make_str_enum("Language", enums.get("language", []))
    WorkflowStatusEnum = make_str_enum("WorkflowStatus", enums.get("workflow_status", []))
    IntakeKindEnum = make_str_enum("IntakeKind", enums.get("intake_kind", []))

    ContentModeT = ContentModeEnum or str
    LanguageT = LanguageEnum or str
    WorkflowStatusT = WorkflowStatusEnum or str
    IntakeKindT = IntakeKindEnum or str

    class MetaParsed(BaseModel):
        model_config = ConfigDict(use_enum_values=True)

        # Canonical URL will be overwritten deterministically in postprocess.
        canonical_url: Optional[str] = Field(
            None,
            description="Canonical URL if present in url metadata; may be null and will be filled by postprocess.",
        )

        # Identity / timing fields
        domain: Optional[str] = None
        site_name: Optional[str] = None
        organization_name: Optional[str] = Field(
            None,
            description="Organization/publisher name from identity_candidates if present, else null.",
        )
        author_names: Optional[List[str]] = Field(
            None,
            description="Author names from identity_candidates if present, else null.",
        )
        published_at: Optional[str] = Field(None, description="ISO 8601 when available, else null.")
        modified_time: Optional[str] = Field(None, description="ISO 8601 when available, else null.")
        collected_at_utc: Optional[str] = Field(None, description="ISO 8601 when available, else null.")

        # Deterministic counts (may be omitted by the model; we fill in postprocess)
        char_count: Optional[int] = None
        word_count: Optional[int] = None

        # SCF select fields (enforced as enums when available)
        content_mode: ContentModeT = Field(..., description="SCF select value.")
        language: Optional[LanguageT] = Field(None, description="SCF select value or null.")
        workflow_status: Optional[WorkflowStatusT] = Field(None, description="SCF select value or null.")
        intake_kind: Optional[IntakeKindT] = Field(None, description="SCF select value or null.")

    return MetaParsed


# ---------------------------
# OpenAI call
# ---------------------------

SYSTEM_PROMPT = """You are xAIO Metadata Extractor.

Return ONLY valid JSON matching the provided schema.
Do not include markdown, commentary, or extra keys.

Rules:
- Do NOT analyze or infer from any full article body (it is intentionally absent).
- Use only the provided URL + HEAD/metadata fields in the input JSON.
- For organization_name and author_names, choose only from meta.identity_candidates (or return null).
- If a value is not present in metadata, return null.
- For SCF select fields, return EXACTLY one of the allowed enum values (or null where allowed).
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
    meta_input: Dict[str, Any],
    reasoning_effort: Optional[str],
) -> Tuple[Optional[BaseModel], Dict[str, Any]]:
    client = OpenAI()
    return structured_parse(
        client,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_content=json.dumps(meta_input, ensure_ascii=False),
        schema=schema,
        reasoning_effort=reasoning_effort,
    )


# ---------------------------
# Deterministic postprocessing
# ---------------------------

def normalize_candidates(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    out: List[str] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, str):
            val = item.strip()
            if val and val not in seen:
                out.append(val)
                seen.add(val)
    return out


def normalize_choice(value: Any, candidates: Sequence[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    val = value.strip()
    if not val:
        return None
    return val if val in candidates else None


def normalize_choice_list(value: Any, candidates: Sequence[str]) -> Optional[List[str]]:
    if not isinstance(value, list):
        return None
    chosen: List[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        val = item.strip()
        if not val or val in seen:
            continue
        if val in candidates:
            chosen.append(val)
            seen.add(val)
    return chosen or None


def postprocess(out: Dict[str, Any], meta_input: Dict[str, Any]) -> Dict[str, Any]:
    # Canonical URL: always prefer the clean canonical if available.
    canon = (
        (((meta_input.get("url") or {}).get("clean") or {}).get("canonical"))
        or (meta_input.get("url") or {}).get("canonical_hint")
        or (meta_input.get("url") or {}).get("final")
        or (meta_input.get("url") or {}).get("original")
    )
    if canon:
        out["canonical_url"] = canon
    else:
        out["canonical_url"] = out.get("canonical_url") or ""

    url = meta_input.get("url") or {}
    meta = meta_input.get("meta") or {}
    mw = meta.get("meta_whitelist") or {}
    cap = meta_input.get("capture") or {}
    content = meta_input.get("content") or {}
    identity = meta.get("identity_candidates") or {}

    org_candidates = normalize_candidates(identity.get("organization_names"))
    author_candidates = normalize_candidates(identity.get("author_names"))

    out["domain"] = url.get("domain") or out.get("domain")
    out["site_name"] = meta.get("site_name") or out.get("site_name")
    out["collected_at_utc"] = cap.get("collected_at_utc") or out.get("collected_at_utc")

    # Timestamps (prefer schema-ish meta_whitelist)
    out["published_at"] = (
        mw.get("article:published_time")
        or meta.get("published_at_hint")
        or out.get("published_at")
    )
    out["modified_time"] = mw.get("article:modified_time") or out.get("modified_time")

    # Counts (cheap + stable)
    if "char_count" in content:
        out["char_count"] = content.get("char_count")
    if "word_count" in content:
        out["word_count"] = content.get("word_count")

    out["organization_name"] = normalize_choice(out.get("organization_name"), org_candidates)
    out["author_names"] = normalize_choice_list(out.get("author_names"), author_candidates)

    return out


# ---------------------------
# CLI
# ---------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("meta_input_json")
    ap.add_argument("--scf-export", default="config/scf-export-content.json")
    ap.add_argument("--outdir", default="./out_meta")
    ap.add_argument("--write-raw", action="store_true")
    ap.add_argument("--model", default="gpt-5-nano")
    ap.add_argument("--reasoning-effort", default="minimal")
    args = ap.parse_args()

    meta_path = Path(args.meta_input_json).expanduser().resolve()
    scf_path = Path(args.scf_export).expanduser().resolve()
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    item_id = meta_path.stem.replace(".meta_input", "")
    start_time = time.monotonic()
    log_event(logger, stage="meta_start", item_id=item_id, message="meta parse starting")

    try:
        meta_input = load_json(meta_path)

        enums = scf_enums_from_export(scf_path)
        Schema = build_model(enums)

        parsed, raw = call_openai_structured(args.model, Schema, meta_input, args.reasoning_effort)
        if parsed is None:
            raw_path = outdir / (item_id + ".meta_response_raw.json")
            raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            raise RuntimeError(f"Parse failure/refusal. Raw: {raw_path}")

        out = postprocess(parsed.model_dump(), meta_input)

        out_path = outdir / f"{item_id}.meta_parsed.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.write_raw:
            raw_path = outdir / f"{item_id}.meta_response_raw.json"
            raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

        log_event(
            logger,
            stage="meta_done",
            item_id=item_id,
            elapsed_ms_value=elapsed_ms(start_time),
            message=f"wrote={out_path}",
        )
        return 0

    except Exception as exc:
        log_event(
            logger,
            stage="meta_failed",
            item_id=item_id,
            elapsed_ms_value=elapsed_ms(start_time),
            message=f"{type(exc).__name__}: {exc}",
            level=logging.ERROR,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
