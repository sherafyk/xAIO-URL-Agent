#!/usr/bin/env python3
"""
ai_queue.py

Pipeline stage 3:
- Reads the Google Sheet queue
- Finds rows where ai_status == AI_READY
- Ensures meta_input exists (no extracted_text_full)
- Runs meta + claims calls
- Merges outputs into xaio_parsed.json
- Writes statuses/paths/errors back to the sheet
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import gspread
import yaml
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from strip_content_for_meta import load_json, write_meta_input
from logging_utils import elapsed_ms, log_event, setup_logging

logger = setup_logging("ai_queue")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def col_letter_to_index(letter: str) -> int:
    letter = letter.strip().upper()
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1  # zero-based


def cell_addr(col_letter: str, row: int) -> str:
    return f"{col_letter}{row}"


def safe(s: str) -> str:
    return (s or "").strip()


def gs_client() -> gspread.Client:
    return gspread.service_account(filename="secrets/service_account.json")


def open_worksheet(gc: gspread.Client, spreadsheet_url: str, worksheet_name: str):
    sh = gc.open_by_url(spreadsheet_url)
    return sh.worksheet(worksheet_name)


def update_cells(wks, row: int, updates: Dict[str, str]) -> None:
    for col, val in updates.items():
        wks.update_acell(cell_addr(col, row), val)


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(Exception),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def update_cells_with_retry(wks, row: int, updates: Dict[str, str]) -> None:
    update_cells(wks, row, updates)


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


@dataclass
class AIQueueConfig:
    spreadsheet_url: str
    worksheet_name: str
    header_row: int
    first_data_row: int

    col_url: str
    col_ai_status: str
    col_ai_input_path: str
    col_ai_error: str

    col_meta_status: str
    col_meta_path: str
    col_meta_error: str

    col_claims_status: str
    col_claims_path: str
    col_claims_error: str

    col_xaio_status: str
    col_xaio_path: str
    col_xaio_error: str

    out_ai_meta_dir: str
    out_meta_dir: str
    out_claims_dir: str
    out_xaio_dir: str

    scf_export_path: str
    meta_model: str
    meta_reasoning_effort: Optional[str]
    claims_model: str
    claims_reasoning_effort: Optional[str]

    max_per_run: int


def load_config(path: str = "config.yaml") -> AIQueueConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    cols = data.get("columns", {})
    cols_ai = data.get("columns_ai", {})

    paths = data.get("paths", {})
    ai_cfg = data.get("ai", {})
    ai_meta = ai_cfg.get("meta", {})
    ai_claims = ai_cfg.get("claims", {})
    ai_queue_cfg = data.get("agent_ai_queue", {})

    return AIQueueConfig(
        spreadsheet_url=data["sheet"]["spreadsheet_url"],
        worksheet_name=data["sheet"]["worksheet_name"],
        header_row=int(data["sheet"].get("header_row", 1)),
        first_data_row=int(data["sheet"].get("first_data_row", 2)),
        col_url=cols.get("url", "A"),
        col_ai_status=cols_ai.get("ai_status", "I"),
        col_ai_input_path=cols_ai.get("ai_input_path", "J"),
        col_ai_error=cols_ai.get("ai_error", "K"),
        col_meta_status=cols_ai.get("meta_status", "L"),
        col_meta_path=cols_ai.get("meta_path", "M"),
        col_meta_error=cols_ai.get("meta_error", "N"),
        col_claims_status=cols_ai.get("claims_status", "O"),
        col_claims_path=cols_ai.get("claims_path", "P"),
        col_claims_error=cols_ai.get("claims_error", "Q"),
        col_xaio_status=cols_ai.get("xaio_status", "R"),
        col_xaio_path=cols_ai.get("xaio_path", "S"),
        col_xaio_error=cols_ai.get("xaio_error", "T"),
        out_ai_meta_dir=paths.get("out_ai_meta_dir", "./out_ai_meta"),
        out_meta_dir=paths.get("out_meta_dir", "./out_meta"),
        out_claims_dir=paths.get("out_claims_dir", "./out_claims"),
        out_xaio_dir=paths.get("out_xaio_dir", "./out_xaio"),
        scf_export_path=ai_cfg.get("scf_export_path", "config/scf-export-content.json"),
        meta_model=ai_meta.get("model", "gpt-5-nano"),
        meta_reasoning_effort=ai_meta.get("reasoning_effort", "minimal"),
        claims_model=ai_claims.get("model", "gpt-5-nano"),
        claims_reasoning_effort=ai_claims.get("reasoning_effort", "minimal"),
        max_per_run=int(ai_queue_cfg.get("max_per_run", 50)),
    )


def content_sha(ai_input: Dict[str, object]) -> str:
    content = ai_input.get("content") if isinstance(ai_input.get("content"), dict) else {}
    if isinstance(content, dict):
        return str(content.get("sha256") or "")
    return ""


def ensure_meta_input(ai_input: Dict[str, object], ai_path: Path, outdir: Path) -> Path:
    base = ai_path.stem.replace(".ai_input", "")
    meta_input_path = outdir / f"{base}.meta_input.json"
    current_sha = content_sha(ai_input)
    if meta_input_path.exists():
        meta_input = load_json(meta_input_path)
        meta_sha = content_sha(meta_input)
        if current_sha and meta_sha == current_sha:
            return meta_input_path
    write_meta_input(ai_input, meta_input_path)
    return meta_input_path


def run_subprocess(cmd: List[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def run_call_openai_meta(meta_input_path: Path, cfg: AIQueueConfig) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).resolve().parent / "call_openai_meta.py"
    cmd = [
        sys.executable,
        str(script),
        str(meta_input_path),
        "--scf-export",
        cfg.scf_export_path,
        "--outdir",
        cfg.out_meta_dir,
        "--model",
        cfg.meta_model,
    ]
    if cfg.meta_reasoning_effort:
        cmd.extend(["--reasoning-effort", cfg.meta_reasoning_effort])
    return run_subprocess(cmd)


def run_call_openai_claims(ai_input_path: Path, meta_parsed_path: Path, cfg: AIQueueConfig) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).resolve().parent / "call_openai_claims.py"
    cmd = [
        sys.executable,
        str(script),
        str(ai_input_path),
        str(meta_parsed_path),
        "--scf-export",
        cfg.scf_export_path,
        "--outdir",
        cfg.out_claims_dir,
        "--model",
        cfg.claims_model,
    ]
    if cfg.claims_reasoning_effort:
        cmd.extend(["--reasoning-effort", cfg.claims_reasoning_effort])
    return run_subprocess(cmd)


def run_merge(ai_input_path: Path, meta_parsed_path: Path, claims_parsed_path: Path, cfg: AIQueueConfig) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).resolve().parent / "merge_xaio.py"
    cmd = [
        sys.executable,
        str(script),
        str(ai_input_path),
        str(meta_parsed_path),
        str(claims_parsed_path),
        "--outdir",
        cfg.out_xaio_dir,
    ]
    return run_subprocess(cmd)


def should_skip_stage(output_path: Path, sha: str) -> bool:
    if not output_path.exists():
        return False
    marker = read_sha_marker(sha_marker_path(output_path))
    return bool(marker and sha and marker == sha)


def mark_stage(output_path: Path, sha: str) -> None:
    if sha:
        write_sha_marker(sha_marker_path(output_path), sha)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)

    gc = gs_client()
    wks = open_worksheet(gc, cfg.spreadsheet_url, cfg.worksheet_name)

    all_vals: List[List[str]] = wks.get_all_values()

    idx_url = col_letter_to_index(cfg.col_url)
    idx_ai_status = col_letter_to_index(cfg.col_ai_status)
    idx_ai_input = col_letter_to_index(cfg.col_ai_input_path)
    idx_meta_status = col_letter_to_index(cfg.col_meta_status)
    idx_claims_status = col_letter_to_index(cfg.col_claims_status)
    idx_xaio_status = col_letter_to_index(cfg.col_xaio_status)

    processed = 0

    out_ai_meta_dir = Path(cfg.out_ai_meta_dir).expanduser().resolve()
    out_ai_meta_dir.mkdir(parents=True, exist_ok=True)
    out_meta_dir = Path(cfg.out_meta_dir).expanduser().resolve()
    out_meta_dir.mkdir(parents=True, exist_ok=True)
    out_claims_dir = Path(cfg.out_claims_dir).expanduser().resolve()
    out_claims_dir.mkdir(parents=True, exist_ok=True)
    out_xaio_dir = Path(cfg.out_xaio_dir).expanduser().resolve()
    out_xaio_dir.mkdir(parents=True, exist_ok=True)

    log_event(logger, stage="run_start", message="ai queue starting")

    for rownum in range(cfg.first_data_row, len(all_vals) + 1):
        row = all_vals[rownum - 1]
        url = safe(row[idx_url] if idx_url < len(row) else "")
        ai_status = safe(row[idx_ai_status] if idx_ai_status < len(row) else "").upper()
        ai_input_path_val = safe(row[idx_ai_input] if idx_ai_input < len(row) else "")
        meta_status = safe(row[idx_meta_status] if idx_meta_status < len(row) else "").upper()
        claims_status = safe(row[idx_claims_status] if idx_claims_status < len(row) else "").upper()
        xaio_status = safe(row[idx_xaio_status] if idx_xaio_status < len(row) else "").upper()

        if ai_status != "AI_READY":
            continue

        if processed >= cfg.max_per_run:
            break

        if not ai_input_path_val:
            safe_update_cells(wks, rownum, {
                cfg.col_ai_status: "AI_FAILED",
                cfg.col_ai_error: "Missing ai_input_path.",
            }, item_id=f"row-{rownum}", url=url)
            continue

        ai_input_path = Path(ai_input_path_val).expanduser().resolve()
        if not ai_input_path.exists():
            safe_update_cells(wks, rownum, {
                cfg.col_ai_status: "AI_FAILED",
                cfg.col_ai_error: f"ai_input not found: {ai_input_path}",
            }, item_id=f"row-{rownum}", url=url)
            continue

        ai_input = load_json(ai_input_path)
        sha = content_sha(ai_input)

        base = ai_input_path.stem.replace(".ai_input", "")
        item_id = base or f"row-{rownum}"
        row_start = time.monotonic()
        log_event(logger, stage="row_start", item_id=item_id, row=rownum, url=url)
        meta_input_path = ensure_meta_input(ai_input, ai_input_path, out_ai_meta_dir)
        meta_parsed_path = out_meta_dir / f"{base}.meta_parsed.json"
        claims_parsed_path = out_claims_dir / f"{base}.claims_parsed.json"
        xaio_parsed_path = out_xaio_dir / f"{base}.xaio_parsed.json"

        if xaio_status == "XAIO_DONE" and should_skip_stage(xaio_parsed_path, sha):
            continue

        if meta_status != "META_DONE" or not should_skip_stage(meta_parsed_path, sha):
            safe_update_cells(wks, rownum, {
                cfg.col_meta_status: "META_RUNNING",
                cfg.col_meta_error: "",
                cfg.col_meta_path: str(meta_parsed_path),
            }, item_id=item_id, url=url)
            res = run_call_openai_meta(meta_input_path, cfg)
            if res.returncode != 0:
                err = (res.stderr or res.stdout or "").strip()
                safe_update_cells(wks, rownum, {
                    cfg.col_meta_status: "META_FAILED",
                    cfg.col_meta_error: err[:49000],
                }, item_id=item_id, url=url)
                log_event(
                    logger,
                    stage="meta_failed",
                    item_id=item_id,
                    row=rownum,
                    url=url,
                    elapsed_ms_value=elapsed_ms(row_start),
                    message=err,
                    level=logging.ERROR,
                )
                continue
            mark_stage(meta_parsed_path, sha)
            safe_update_cells(wks, rownum, {
                cfg.col_meta_status: "META_DONE",
                cfg.col_meta_path: str(meta_parsed_path),
                cfg.col_meta_error: "",
            }, item_id=item_id, url=url)

        if claims_status != "CLAIMS_DONE" or not should_skip_stage(claims_parsed_path, sha):
            safe_update_cells(wks, rownum, {
                cfg.col_claims_status: "CLAIMS_RUNNING",
                cfg.col_claims_error: "",
                cfg.col_claims_path: str(claims_parsed_path),
            }, item_id=item_id, url=url)
            res = run_call_openai_claims(ai_input_path, meta_parsed_path, cfg)
            if res.returncode != 0:
                err = (res.stderr or res.stdout or "").strip()
                safe_update_cells(wks, rownum, {
                    cfg.col_claims_status: "CLAIMS_FAILED",
                    cfg.col_claims_error: err[:49000],
                }, item_id=item_id, url=url)
                log_event(
                    logger,
                    stage="claims_failed",
                    item_id=item_id,
                    row=rownum,
                    url=url,
                    elapsed_ms_value=elapsed_ms(row_start),
                    message=err,
                    level=logging.ERROR,
                )
                continue
            mark_stage(claims_parsed_path, sha)
            safe_update_cells(wks, rownum, {
                cfg.col_claims_status: "CLAIMS_DONE",
                cfg.col_claims_path: str(claims_parsed_path),
                cfg.col_claims_error: "",
            }, item_id=item_id, url=url)

        if xaio_status != "XAIO_DONE" or not should_skip_stage(xaio_parsed_path, sha):
            safe_update_cells(wks, rownum, {
                cfg.col_xaio_status: "XAIO_RUNNING",
                cfg.col_xaio_error: "",
                cfg.col_xaio_path: str(xaio_parsed_path),
            }, item_id=item_id, url=url)
            res = run_merge(ai_input_path, meta_parsed_path, claims_parsed_path, cfg)
            if res.returncode != 0:
                err = (res.stderr or res.stdout or "").strip()
                safe_update_cells(wks, rownum, {
                    cfg.col_xaio_status: "XAIO_FAILED",
                    cfg.col_xaio_error: err[:49000],
                }, item_id=item_id, url=url)
                log_event(
                    logger,
                    stage="xaio_failed",
                    item_id=item_id,
                    row=rownum,
                    url=url,
                    elapsed_ms_value=elapsed_ms(row_start),
                    message=err,
                    level=logging.ERROR,
                )
                continue
            mark_stage(xaio_parsed_path, sha)
            safe_update_cells(wks, rownum, {
                cfg.col_xaio_status: "XAIO_DONE",
                cfg.col_xaio_path: str(xaio_parsed_path),
                cfg.col_xaio_error: "",
            }, item_id=item_id, url=url)

        processed += 1
        log_event(
            logger,
            stage="row_done",
            item_id=item_id,
            row=rownum,
            url=url,
            elapsed_ms_value=elapsed_ms(row_start),
            message="ai queue complete",
        )

    log_event(logger, stage="run_complete", message=f"processed={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())