#!/usr/bin/env python3
"""
strip_content_for_meta.py

Create meta-only input by removing extracted_text_full from ai_input JSON.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from logging_utils import elapsed_ms, log_event, setup_logging

logger = setup_logging("strip_content_for_meta")

def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build_meta_input(ai_input: Dict[str, Any]) -> Dict[str, Any]:
    meta_input = deepcopy(ai_input)
    content = meta_input.get("content")
    if isinstance(content, dict):
        content.pop("extracted_text_full", None)
    return meta_input


def write_meta_input(ai_input: Dict[str, Any], out_path: Path) -> None:
    meta_input = build_meta_input(ai_input)
    out_path.write_text(json.dumps(meta_input, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Strip full text for meta-only AI input.")
    ap.add_argument("--in", dest="in_path", required=True, help="Path to out_ai/<id>.ai_input.json")
    ap.add_argument("--outdir", required=True, help="Directory to write out_ai_meta/<id>.meta_input.json")
    args = ap.parse_args()

    in_path = Path(args.in_path).expanduser().resolve()
    item_id = in_path.stem.replace(".ai_input", "")
    start_time = time.monotonic()
    log_event(logger, stage="meta_input_start", item_id=item_id, message="meta input build starting")

    try:
        outdir = Path(args.outdir).expanduser().resolve()
        outdir.mkdir(parents=True, exist_ok=True)

        ai_input = load_json(in_path)
        base = in_path.stem.replace(".ai_input", "")
        out_path = outdir / f"{base}.meta_input.json"
        write_meta_input(ai_input, out_path)

        log_event(
            logger,
            stage="meta_input_done",
            item_id=item_id,
            elapsed_ms_value=elapsed_ms(start_time),
            message=f"wrote={out_path}",
        )
        return 0
    except Exception as exc:
        log_event(
            logger,
            stage="meta_input_failed",
            item_id=item_id,
            elapsed_ms_value=elapsed_ms(start_time),
            message=f"{type(exc).__name__}: {exc}",
            level=logging.ERROR,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
