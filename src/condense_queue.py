#!/usr/bin/env python3
"""
condense_queue.py

Pipeline stage 2:
- Reads the Google Sheet queue
- Finds rows where status == DONE (col B) and ai_status is blank (col I)
- Uses json_path (col F) to run reduce4ai.py
- Writes ai_status/ai_input_path/ai_error back to the sheet

This does NOT modify your existing capture JSON generation.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import gspread
import yaml


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
    # small volume; cell-by-cell is fine for v1
    for col, val in updates.items():
        wks.update_acell(cell_addr(col, row), val)


@dataclass
class CondenseConfig:
    spreadsheet_url: str
    worksheet_name: str
    header_row: int
    first_data_row: int

    col_url: str
    col_status: str
    col_json_path: str

    col_ai_status: str
    col_ai_input_path: str
    col_ai_error: str

    out_ai_dir: str
    prompt_set_id: str
    max_per_run: int


def load_config(path: str = "config.yaml") -> CondenseConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Existing columns from your main pipeline config
    col_url = data["columns"]["url"]          # A
    col_status = data["columns"]["status"]    # B
    col_json_path = data["columns"]["json_path"]  # F (confirmed)

    # New columns (we will default to I/J/K)
    cols2 = data.get("columns_ai", {})  # optional
    col_ai_status = cols2.get("ai_status", "I")
    col_ai_input_path = cols2.get("ai_input_path", "J")
    col_ai_error = cols2.get("ai_error", "K")

    agent2 = data.get("agent_ai", {})
    out_ai_dir = agent2.get("out_ai_dir", "./out_ai")
    prompt_set_id = agent2.get("prompt_set_id", "xaio-v1-claims+scores")
    max_per_run = int(agent2.get("max_per_run", 50))

    return CondenseConfig(
        spreadsheet_url=data["sheet"]["spreadsheet_url"],
        worksheet_name=data["sheet"]["worksheet_name"],
        header_row=int(data["sheet"].get("header_row", 1)),
        first_data_row=int(data["sheet"].get("first_data_row", 2)),
        col_url=col_url,
        col_status=col_status,
        col_json_path=col_json_path,
        col_ai_status=col_ai_status,
        col_ai_input_path=col_ai_input_path,
        col_ai_error=col_ai_error,
        out_ai_dir=out_ai_dir,
        prompt_set_id=prompt_set_id,
        max_per_run=max_per_run,
    )


def run_reduce4ai(capture_json_path: str, out_ai_dir: str, prompt_set_id: str) -> Tuple[bool, str]:
    """
    Calls reduce4ai.py as a subprocess (safe; avoids refactoring your working code).
    Returns (success, message_or_output_path).
    """
    capture_path = Path(capture_json_path).expanduser()

    if not capture_path.exists():
        return False, f"capture json not found: {capture_path}"

    outdir = Path(out_ai_dir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    reduce4ai_path = Path(__file__).resolve().parent / "reduce4ai.py"
    cmd = [
        sys.executable,
        str(reduce4ai_path),
        str(capture_path),
        "--outdir",
        str(outdir),
        "--prompt-set-id",
        prompt_set_id,
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip()
            return False, f"reduce4ai failed: {err[:2000]}"
        # reduce4ai prints "Wrote AI envelope: <path>"
        out_text = (res.stdout or "").strip().splitlines()
        out_path = ""
        for line in out_text:
            if line.startswith("Wrote AI envelope:"):
                out_path = line.split("Wrote AI envelope:", 1)[1].strip()
                break
        return True, out_path or "ok"
    except Exception as e:
        return False, f"reduce4ai exception: {type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)

    gc = gs_client()
    wks = open_worksheet(gc, cfg.spreadsheet_url, cfg.worksheet_name)

    all_vals: List[List[str]] = wks.get_all_values()

    idx_status = col_letter_to_index(cfg.col_status)
    idx_json = col_letter_to_index(cfg.col_json_path)
    idx_ai_status = col_letter_to_index(cfg.col_ai_status)

    processed = 0

    for rownum in range(cfg.first_data_row, len(all_vals) + 1):
        row = all_vals[rownum - 1]

        status = safe(row[idx_status] if idx_status < len(row) else "").upper()
        ai_status = safe(row[idx_ai_status] if idx_ai_status < len(row) else "").upper()
        json_path = safe(row[idx_json] if idx_json < len(row) else "")

        if status != "DONE":
            continue
        if ai_status in ("AI_READY", "CONDENSING"):
            continue
        if not json_path:
            # capture missing path: mark failed for visibility
            update_cells(wks, rownum, {
                cfg.col_ai_status: "AI_FAILED",
                cfg.col_ai_error: "Missing json_path (col F).",
            })
            continue

        if processed >= cfg.max_per_run:
            break

        # Claim row for condensing
        update_cells(wks, rownum, {
            cfg.col_ai_status: "CONDENSING",
            cfg.col_ai_error: "",
        })

        ok, msg = run_reduce4ai(json_path, cfg.out_ai_dir, cfg.prompt_set_id)

        if ok:
            update_cells(wks, rownum, {
                cfg.col_ai_status: "AI_READY",
                cfg.col_ai_input_path: msg,
                cfg.col_ai_error: "",
            })
        else:
            update_cells(wks, rownum, {
                cfg.col_ai_status: "AI_FAILED",
                cfg.col_ai_error: msg[:49000],
            })

        processed += 1

    print(f"[{now_iso()}] condense_queue complete; processed={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
