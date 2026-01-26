#!/usr/bin/env python3
"""call_openai_meta.py

Pipeline stage 3a (AI metadata):
- Reads a *.meta_input.json (created from *.ai_input.json but WITHOUT full body text)
- Calls OpenAI Chat Completions for unstructured metadata notes
- Writes *.meta_parsed.json with raw text plus deterministic fields

Key design goals:
- Plain text output (no schemas, no validation)
- Resilient: write output even if the model call fails
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from env_bootstrap import load_repo_env
from logging_utils import elapsed_ms, log_event, setup_logging
from openai_compat import chat_completion_text

load_repo_env()
logger = setup_logging("call_openai_meta")


# ---------------------------
# IO helpers
# ---------------------------

def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def first_str(*vals: Any) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


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


def deterministic_fields(meta_input: Dict[str, Any]) -> Dict[str, Any]:
    url = meta_input.get("url") or {}
    meta = meta_input.get("meta") or {}
    mw = meta.get("meta_whitelist") or {}
    cap = meta_input.get("capture") or {}

    canonical_url = first_str(
        ((url.get("clean") or {}).get("canonical")),
        url.get("canonical_hint"),
        url.get("final"),
        url.get("original"),
    )

    identity = meta.get("identity_candidates") or {}
    org_candidates = normalize_candidates(identity.get("organization_names"))
    author_candidates = normalize_candidates(identity.get("author_names"))

    author_hint = first_str(meta.get("author_hint"))
    if not author_candidates and author_hint:
        author_candidates = [author_hint]

    organization_name = first_str(org_candidates[0] if org_candidates else "", meta.get("site_name"))

    title = first_str(meta.get("title"))

    return {
        "canonical_url": canonical_url,
        "domain": first_str(url.get("domain")),
        "site_name": first_str(meta.get("site_name")),
        "organization_name": organization_name,
        "author_names": author_candidates,
        "published_at": first_str(mw.get("article:published_time"), meta.get("published_at_hint")),
        "modified_time": first_str(mw.get("article:modified_time")),
        "collected_at_utc": first_str(cap.get("collected_at_utc")),
        "content_mode": "url",
        "url_content_title": title,
    }


# ---------------------------
# OpenAI call
# ---------------------------

SYSTEM_PROMPT = """You are xAIO Metadata Notes.

Return a concise plain-text summary of any useful metadata signals from the input JSON.
Do not output JSON. Do not use markdown. Keep it brief and literal."""


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((RateLimitError, APITimeoutError, APIConnectionError, APIError)),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def call_openai_text(
    model: str,
    meta_input: Dict[str, Any],
) -> str:
    client = OpenAI()
    return chat_completion_text(
        client,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_content=json.dumps(meta_input, ensure_ascii=False),
    )


# ---------------------------
# CLI
# ---------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("meta_input_json")
    ap.add_argument("--outdir", default="./out_meta")
    ap.add_argument("--write-raw", action="store_true")
    ap.add_argument("--model", default="gpt-5-nano")
    args = ap.parse_args()

    meta_path = Path(args.meta_input_json).expanduser().resolve()
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    item_id = meta_path.stem.replace(".meta_input", "")
    start_time = time.monotonic()
    log_event(logger, stage="meta_start", item_id=item_id, message="meta parse starting")

    try:
        meta_input = load_json(meta_path)

        raw_text = ""
        try:
            raw_text = call_openai_text(args.model, meta_input)
        except Exception as exc:
            logger.warning(f"meta call failed item={item_id} err={exc}")

        out: Dict[str, Any] = deterministic_fields(meta_input)
        out["meta"] = raw_text

        out_path = outdir / f"{item_id}.meta_parsed.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.write_raw:
            raw_path = outdir / f"{item_id}.meta_response_raw.txt"
            raw_path.write_text(raw_text or "", encoding="utf-8")

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
