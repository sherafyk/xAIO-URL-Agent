#!/usr/bin/env python3
"""call_openai_claims.py

Pipeline stage 3b (AI claims):
- Reads a *.ai_input.json (WITH extracted_text_full)
- Reads the matching *.meta_parsed.json (unused but kept for interface)
- Calls OpenAI Chat Completions to extract claims as plain text
- Writes *.claims_parsed.json with raw text

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
logger = setup_logging("call_openai_claims")


# ---------------------------
# IO helpers
# ---------------------------

def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------
# OpenAI call
# ---------------------------

SYSTEM_PROMPT = """You are xAIO Claims Notes.

Return plain text claims derived from extracted_text_full.
Do not output JSON. Do not use markdown. One claim per line if possible."""


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((RateLimitError, APITimeoutError, APIConnectionError, APIError)),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def call_openai_text(
    model: str,
    user_input: Dict[str, Any],
) -> str:
    client = OpenAI()
    return chat_completion_text(
        client,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_content=json.dumps(user_input, ensure_ascii=False),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ai_input_json")
    ap.add_argument("meta_parsed_json")
    ap.add_argument("--outdir", default="./out_claims")
    ap.add_argument("--write-raw", action="store_true")
    ap.add_argument("--model", default="gpt-5-nano")
    args = ap.parse_args()

    ai_path = Path(args.ai_input_json).expanduser().resolve()
    meta_path = Path(args.meta_parsed_json).expanduser().resolve()
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    item_id = ai_path.stem.replace(".ai_input", "")
    start_time = time.monotonic()
    log_event(logger, stage="claims_start", item_id=item_id, message="claims parse starting")

    try:
        ai_input = load_json(ai_path)
        _meta = load_json(meta_path)

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

        raw_text = ""
        try:
            raw_text = call_openai_text(args.model, user_input)
        except Exception as exc:
            logger.warning(f"claims call failed item={item_id} err={exc}")

        out = {"claims": raw_text}

        out_path = outdir / f"{item_id}.claims_parsed.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.write_raw:
            raw_path = outdir / f"{item_id}.claims_response_raw.txt"
            raw_path.write_text(raw_text or "", encoding="utf-8")

        log_event(
            logger,
            stage="claims_done",
            item_id=item_id,
            elapsed_ms_value=elapsed_ms(start_time),
            message=f"wrote={out_path}",
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
