#!/usr/bin/env python3
"""pipeline_run.py

Runs the full xAIO pipeline in order:
  1) agent.py           (capture URLs -> out/*.json)
  2) condense_queue.py  (reduce captures -> out_ai/*.ai_input.json)
  3) ai_queue.py        (OpenAI meta + claims -> out_xaio/*.xaio_parsed.json)
  4) wp_upload_queue.py (publish -> WordPress)

This wrapper exists so you can run one command locally or via systemd timers.
It passes a single, explicit config path to every stage.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Tuple

from env_bootstrap import load_repo_env
from logging_utils import elapsed_ms, log_event, setup_logging

load_repo_env()
logger = setup_logging("pipeline_run")


STAGES: List[Tuple[str, str]] = [
    ("capture", "agent.py"),
    ("condense", "condense_queue.py"),
    ("ai", "ai_queue.py"),
    ("wp", "wp_upload_queue.py"),
]


def run_stage(script_name: str, config_path: Path) -> subprocess.CompletedProcess[str]:
    script_path = Path(__file__).resolve().parent / script_name
    cmd = [sys.executable, str(script_path), "--config", str(config_path)]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config",
        default=os.getenv(
            "XAIO_CONFIG_PATH",
            str(Path(sys.argv[0]).resolve().parent.parent / ".runtime" / "config.yaml"),
        ),
        help="Path to runtime config.yaml",
    )
    ap.add_argument(
        "--stages",
        default=",".join([name for name, _ in STAGES]),
        help="Comma-separated subset of stages to run (capture,condense,ai,wp)",
    )
    args = ap.parse_args()

    config_path = Path(str(args.config)).expanduser().resolve()
    allowed = {name for name, _ in STAGES}
    requested = [s.strip() for s in str(args.stages).split(",") if s.strip()]
    requested = [s for s in requested if s in allowed]

    if not requested:
        logger.error("No valid stages requested.")
        return 2

    start = time.monotonic()
    log_event(logger, stage="pipeline_start", message=f"stages={requested} config={config_path}")

    for stage_name, script in STAGES:
        if stage_name not in requested:
            continue

        t0 = time.monotonic()
        log_event(logger, stage="stage_start", message=f"{stage_name} -> {script}")

        res = run_stage(script, config_path)
        if res.returncode != 0:
            msg = (res.stderr or res.stdout or "").strip()
            log_event(
                logger,
                stage="stage_failed",
                message=f"{stage_name} rc={res.returncode} err={msg}",
                level=logging.ERROR,
                elapsed_ms_value=elapsed_ms(t0),
            )
            return res.returncode

        log_event(
            logger,
            stage="stage_done",
            message=f"{stage_name} ok",
            elapsed_ms_value=elapsed_ms(t0),
        )

    log_event(logger, stage="pipeline_done", message="ok", elapsed_ms_value=elapsed_ms(start))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
