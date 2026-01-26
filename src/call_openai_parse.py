#!/usr/bin/env python3
"""
call_openai_parse.py

Reads an ai_input.json and calls OpenAI Chat Completions to produce a
plain-text xAIO output. Writes xaio_parsed.json with raw text plus
basic deterministic fields derived from the input.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict

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

logger = setup_logging("call_openai_parse")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def first_str(*vals: Any) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def deterministic_fields(ai_input: Dict[str, Any]) -> Dict[str, Any]:
    url = ai_input.get("url") if isinstance(ai_input.get("url"), dict) else {}
    meta = ai_input.get("meta") if isinstance(ai_input.get("meta"), dict) else {}
    content = ai_input.get("content") if isinstance(ai_input.get("content"), dict) else {}

    identity = meta.get("identity_candidates") or {}
    org_names = identity.get("organization_names") if isinstance(identity, dict) else []
    author_names = identity.get("author_names") if isinstance(identity, dict) else []

    author_hint = first_str(meta.get("author_hint"))
    if not author_names and author_hint:
        author_names = [author_hint]

    canonical_url = first_str(
        ((url.get("clean") or {}).get("canonical")),
        url.get("canonical_hint"),
        url.get("final"),
        url.get("original"),
    )

    return {
        "canonical_url": canonical_url,
        "domain": first_str(url.get("domain")),
        "site_name": first_str(meta.get("site_name")),
        "organization_name": first_str(org_names[0] if org_names else "", meta.get("site_name")),
        "author_names": author_names if isinstance(author_names, list) else [],
        "url_content_title": first_str(meta.get("title")),
        "content_mode": "url",
        "extracted_text_full": first_str(content.get("extracted_text_full")),
        "char_count": content.get("char_count"),
        "word_count": content.get("word_count"),
    }


SYSTEM_PROMPT = """You are xAIO Extractor.

Return a concise plain-text summary of the input content and any key claims.
Do not output JSON. Do not use markdown."""


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((RateLimitError, APITimeoutError, APIConnectionError, APIError)),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def call_openai_text(
    model: str,
    ai_input: Dict[str, Any],
) -> str:
    client = OpenAI()
    return chat_completion_text(
        client,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_content=json.dumps(ai_input, ensure_ascii=False),
    )


# ---------------------------
# CLI
# ---------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Call OpenAI to parse ai_input.json into xaio_parsed.json (plain text).")
    ap.add_argument("ai_input_json", help="Path to *.ai_input.json produced by reduce4ai.py")
    ap.add_argument("--outdir", default="./out_xaio", help="Where to write *.xaio_parsed.json")
    ap.add_argument("--write-raw", action="store_true", help="Also write a *.xaio_response_raw.txt debugging file")
    ap.add_argument("--model", default="gpt-5-nano", help="OpenAI model to use (default: gpt-5-nano)")

    args = ap.parse_args()

    ai_input_path = Path(args.ai_input_json).expanduser().resolve()
    if not ai_input_path.exists():
        raise FileNotFoundError(f"ai_input not found: {ai_input_path}")

    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    item_id = ai_input_path.stem.replace(".ai_input", "")
    start_time = time.monotonic()
    log_event(logger, stage="parse_start", item_id=item_id, message="parse starting")

    try:
        ai_input = load_json(ai_input_path)

        raw_text = ""
        try:
            raw_text = call_openai_text(model=args.model, ai_input=ai_input)
        except Exception as exc:
            logger.warning(f"parse call failed item={item_id} err={exc}")

        out = deterministic_fields(ai_input)
        out["xaio"] = raw_text
        out["claims"] = raw_text

        base = ai_input_path.stem.replace(".ai_input", "")
        out_path = outdir / f"{base}.xaio_parsed.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.write_raw:
            raw_path = outdir / f"{base}.xaio_response_raw.txt"
            raw_path.write_text(raw_text or "", encoding="utf-8")

        log_event(
            logger,
            stage="parse_done",
            item_id=item_id,
            elapsed_ms_value=elapsed_ms(start_time),
            message=f"wrote={out_path}",
        )
        return 0
    except Exception as exc:
        log_event(
            logger,
            stage="parse_failed",
            item_id=item_id,
            elapsed_ms_value=elapsed_ms(start_time),
            message=f"{type(exc).__name__}: {exc}",
            level=logging.ERROR,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
