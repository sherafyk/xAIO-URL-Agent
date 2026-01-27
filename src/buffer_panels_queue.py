#!/usr/bin/env python3
"""buffer_panels_queue.py

Pipeline stage (NEW): Buffered panel extraction

Reads the Google Sheet queue and, for each row where:
  - xaio_status == XAIO_DONE
  - buffers_status is blank or failed

...runs call_openai_buffers.py to generate the 12 "buffered panels" (Option 2)
and writes:
  - buffers_status
  - buffers_path
  - buffers_error

This keeps the WordPress upload stage simple: wp_upload_queue.py can require
BUFFERS_DONE before publishing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import gspread
import yaml
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from env_bootstrap import load_repo_env
from logging_utils import elapsed_ms, log_event, setup_logging
from sheets_batch import batch_update_row_cells


load_repo_env()
logger = setup_logging("buffer_panels_queue")


def col_letter_to_index(letter: str) -> int:
    letter = letter.strip().upper()
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def safe(s: str) -> str:
    return (s or "").strip()


def gs_client() -> gspread.Client:
    sa_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", ".runtime/secrets/service_account.json")
    return gspread.service_account(filename=sa_path)


def open_worksheet(gc: gspread.Client, spreadsheet_url: str, worksheet_name: str):
    sh = gc.open_by_url(spreadsheet_url)
    return sh.worksheet(worksheet_name)


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(Exception),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def update_cells_with_retry(wks, row: int, updates: Dict[str, str]) -> None:
    batch_update_row_cells(wks, row, updates)


def safe_update_cells(wks, row: int, updates: Dict[str, str], *, item_id: str, url: str) -> None:
    try:
        update_cells_with_retry(wks, row, updates)
    except Exception as exc:
        log_event(
            logger,
            stage="sheet_update_failed",
            item_id=item_id,
            row=row,
            url=url,
            message=f"{type(exc).__name__}: {exc}",
            level=logging.ERROR,
        )


def sha_marker_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".sha256")


def read_sha_marker(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def write_sha_marker(path: Path, sha: str) -> None:
    path.write_text(sha, encoding="utf-8")


def should_skip_stage(output_path: Path, sha: str) -> bool:
    if not output_path.exists():
        return False
    marker = read_sha_marker(sha_marker_path(output_path))
    return bool(marker and sha and marker == sha)


def mark_stage(output_path: Path, sha: str) -> None:
    if sha:
        write_sha_marker(sha_marker_path(output_path), sha)


def sha256_hex(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class BuffersQueueConfig:
    spreadsheet_url: str
    worksheet_name: str
    header_row: int
    first_data_row: int

    col_url: str

    col_xaio_status: str
    col_xaio_path: str

    col_buffers_status: str
    col_buffers_path: str
    col_buffers_error: str

    out_buffers_dir: str

    model: str
    schema_version: str
    max_chars: int

    max_per_run: int


def load_config(path: str = "config.yaml") -> BuffersQueueConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    cols = data.get("columns", {})
    cols_ai = data.get("columns_ai", {})
    cols_buf = data.get("columns_buffers", {})
    paths = data.get("paths", {})
    ai = data.get("ai", {})
    buf_ai = ai.get("buffers", {})
    qcfg = data.get("agent_buffers_queue", {})

    return BuffersQueueConfig(
        spreadsheet_url=data["sheet"]["spreadsheet_url"],
        worksheet_name=data["sheet"]["worksheet_name"],
        header_row=int(data["sheet"].get("header_row", 1)),
        first_data_row=int(data["sheet"].get("first_data_row", 2)),

        col_url=cols.get("url", "A"),

        col_xaio_status=cols_ai.get("xaio_status", "R"),
        col_xaio_path=cols_ai.get("xaio_path", "S"),

        col_buffers_status=cols_buf.get("buffers_status", "X"),
        col_buffers_path=cols_buf.get("buffers_path", "Y"),
        col_buffers_error=cols_buf.get("buffers_error", "Z"),

        out_buffers_dir=paths.get("out_buffers_dir", "./out_buffers"),

        model=buf_ai.get("model", "gpt-4.1-mini"),
        schema_version=str(buf_ai.get("schema_version", "0.1.0")),
        max_chars=int(buf_ai.get("max_chars", 120_000)),

        max_per_run=int(qcfg.get("max_per_run", 30)),
    )


def run_subprocess(cmd: List[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def run_call_openai_buffers(xaio_parsed_path: Path, *, outdir: str, model: str, schema_version: str, max_chars: int) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).resolve().parent / "call_openai_buffers.py"
    cmd = [
        sys.executable,
        str(script),
        str(xaio_parsed_path),
        "--outdir",
        outdir,
        "--model",
        model,
        "--schema-version",
        schema_version,
        "--max-chars",
        str(int(max_chars)),
    ]
    return run_subprocess(cmd)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.getenv("XAIO_CONFIG_PATH", ".runtime/config.yaml"))
    args = ap.parse_args()

    cfg = load_config(args.config)

    gc = gs_client()
    wks = open_worksheet(gc, cfg.spreadsheet_url, cfg.worksheet_name)

    url_values = wks.col_values(col_letter_to_index(cfg.col_url))
    xaio_status_values = wks.col_values(col_letter_to_index(cfg.col_xaio_status))
    xaio_path_values = wks.col_values(col_letter_to_index(cfg.col_xaio_path))
    buf_status_values = wks.col_values(col_letter_to_index(cfg.col_buffers_status))
    buf_path_values = wks.col_values(col_letter_to_index(cfg.col_buffers_path))

    max_rows = max(
        len(url_values),
        len(xaio_status_values),
        len(xaio_path_values),
        len(buf_status_values),
        len(buf_path_values),
    )

    outdir = Path(cfg.out_buffers_dir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    processed = 0
    log_event(logger, stage="run_start", message="buffers queue starting")

    for rownum in range(cfg.first_data_row, max_rows + 1):
        if processed >= cfg.max_per_run:
            break

        row_idx = rownum - 1
        url = safe(url_values[row_idx] if row_idx < len(url_values) else "")

        xaio_status = safe(xaio_status_values[row_idx] if row_idx < len(xaio_status_values) else "").upper()
        xaio_path_s = safe(xaio_path_values[row_idx] if row_idx < len(xaio_path_values) else "")
        buf_status = safe(buf_status_values[row_idx] if row_idx < len(buf_status_values) else "").upper()
        buf_path_s = safe(buf_path_values[row_idx] if row_idx < len(buf_path_values) else "")

        if xaio_status != "XAIO_DONE":
            continue

        # If already done and marker matches, skip.
        if xaio_path_s:
            item_id = Path(xaio_path_s).stem.replace(".xaio_parsed", "")
        else:
            item_id = f"row-{rownum}"

        if not xaio_path_s:
            safe_update_cells(
                wks,
                rownum,
                {
                    cfg.col_buffers_status: "BUFFERS_FAILED",
                    cfg.col_buffers_error: "Missing xaio_path.",
                },
                item_id=item_id,
                url=url,
            )
            continue

        xaio_path = Path(xaio_path_s).expanduser().resolve()
        if not xaio_path.exists():
            safe_update_cells(
                wks,
                rownum,
                {
                    cfg.col_buffers_status: "BUFFERS_FAILED",
                    cfg.col_buffers_error: f"xaio_parsed not found: {xaio_path}",
                },
                item_id=item_id,
                url=url,
            )
            continue

        # Compute expected output path.
        expected_out = outdir / f"{item_id}.buffers.json"

        # Compute sha on the extracted text (cheap + deterministic).
        sha = ""
        try:
            xaio = load_json(xaio_path)
            sha = sha256_hex(str(xaio.get("extracted_text_full") or ""))
        except Exception:
            sha = ""

        if buf_status == "BUFFERS_DONE" and should_skip_stage(expected_out, sha):
            continue

        # Only process when blank or failed; don't stomp if someone manually sets RUNNING.
        if buf_status and buf_status not in ("", "BUFFERS_FAILED"):
            continue

        row_start = time.monotonic()
        log_event(logger, stage="row_start", item_id=item_id, row=rownum, url=url)

        safe_update_cells(
            wks,
            rownum,
            {
                cfg.col_buffers_status: "BUFFERS_RUNNING",
                cfg.col_buffers_path: str(expected_out),
                cfg.col_buffers_error: "",
            },
            item_id=item_id,
            url=url,
        )

        res = run_call_openai_buffers(
            xaio_path,
            outdir=str(outdir),
            model=cfg.model,
            schema_version=cfg.schema_version,
            max_chars=cfg.max_chars,
        )

        if res.returncode != 0 or not expected_out.exists():
            err = (res.stderr or res.stdout or "").strip()
            if not err:
                err = "buffers stage failed (no stderr/stdout)"
            safe_update_cells(
                wks,
                rownum,
                {
                    cfg.col_buffers_status: "BUFFERS_FAILED",
                    cfg.col_buffers_error: err[:49000],
                },
                item_id=item_id,
                url=url,
            )
            log_event(
                logger,
                stage="row_failed",
                item_id=item_id,
                row=rownum,
                url=url,
                elapsed_ms_value=elapsed_ms(row_start),
                message=err,
                level=logging.ERROR,
            )
            processed += 1
            continue

        mark_stage(expected_out, sha)
        safe_update_cells(
            wks,
            rownum,
            {
                cfg.col_buffers_status: "BUFFERS_DONE",
                cfg.col_buffers_path: str(expected_out),
                cfg.col_buffers_error: "",
            },
            item_id=item_id,
            url=url,
        )

        log_event(
            logger,
            stage="row_done",
            item_id=item_id,
            row=rownum,
            url=url,
            elapsed_ms_value=elapsed_ms(row_start),
            message="buffers queue complete",
        )
        processed += 1

    log_event(logger, stage="run_complete", message=f"processed={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
