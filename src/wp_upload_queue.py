#!/usr/bin/env python3
"""
wp_upload_queue.py

Pipeline stage 4:
- Reads the Google Sheet queue
- Finds rows where xaio_status == XAIO_DONE and wp_status is blank/failed
- Loads xaio_parsed.json (col xaio_path)
- Builds payload for MU plugin /wp-json/xaio/v1/ingest
- Writes wp_status/wp_post_id/wp_error back to sheet
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml

from logging_utils import elapsed_ms, log_event, setup_logging

from env_bootstrap import load_repo_env
load_repo_env()

logger = setup_logging("wp_upload_queue")


# ---------------------------
# Helpers: sheet utils copied-style
# ---------------------------

def col_letter_to_index(col: str) -> int:
    col = col.strip().upper()
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1

def get_cell(row: List[str], idx: int) -> str:
    if idx < 0 or idx >= len(row):
        return ""
    return (row[idx] or "").strip()

def safe_str(x: Any) -> str:
    return "" if x is None else str(x)

def normalize_domain(domain: str) -> str:
    d = (domain or "").strip()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0]
    return d


# ---------------------------
# Config
# ---------------------------

@dataclass
class WPQueueConfig:
    spreadsheet_url: str
    worksheet_name: str

    col_xaio_status: str
    col_xaio_path: str
    col_xaio_error: str

    col_wp_status: str
    col_wp_post_id: str
    col_wp_error: str

    wp_base_url: str
    wp_ingest_path: str
    wp_post_status: str
    topics_mode: str
    topics_max: int

    max_per_run: int


def load_config(path: str = "config.yaml") -> WPQueueConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    sheet = data["sheet"]
    cols_ai = data.get("columns_ai", {})
    cols_wp = data.get("columns_wp", {})
    wp = data.get("wordpress", {})

    # XAIO columns (from your ai_queue stage)
    col_xaio_status = cols_ai.get("xaio_status", "Q")
    col_xaio_path = cols_ai.get("xaio_path", "R")
    col_xaio_error = cols_ai.get("xaio_error", "S")

    # WP columns
    col_wp_status = cols_wp.get("wp_status", "U")
    col_wp_post_id = cols_wp.get("wp_post_id", "V")
    col_wp_error = cols_wp.get("wp_error", "W")

    return WPQueueConfig(
        spreadsheet_url=sheet["spreadsheet_url"],
        worksheet_name=sheet.get("worksheet_name", "Queue"),

        col_xaio_status=col_xaio_status,
        col_xaio_path=col_xaio_path,
        col_xaio_error=col_xaio_error,

        col_wp_status=col_wp_status,
        col_wp_post_id=col_wp_post_id,
        col_wp_error=col_wp_error,

        wp_base_url=wp["base_url"],
        wp_ingest_path=wp.get("ingest_path", "/wp-json/xaio/v1/ingest"),
        wp_post_status=wp.get("wp_post_status", "draft"),
        topics_mode=wp.get("topics_mode", "simple"),
        topics_max=int(wp.get("topics_max", 8)),

        max_per_run=int(wp.get("max_per_run", 50)),
    )


# ---------------------------
# Google Sheets client (same libs you already use)
# ---------------------------

def gs_client():
    import gspread
    from google.oauth2.service_account import Credentials

    sa_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "secrets/service_account.json")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(sa_path, scopes=scopes)
    return gspread.authorize(creds)

def open_worksheet(gc, spreadsheet_url: str, worksheet_name: str):
    sh = gc.open_by_url(spreadsheet_url)
    return sh.worksheet(worksheet_name)

def safe_update_cells(wks, row: int, updates: Dict[str, Any], *, item_id: str, url: str):
    """
    You already implemented Tier A batching elsewhere; keep consistent:
    - If you made a shared batch helper, import and use it here.
    - Otherwise fallback to per-cell update.
    """
    try:
        # If you created a batch helper, use it:
        from sheets_batch import batch_update_row_cells
        batch_update_row_cells(wks, row, updates)
    except Exception:
        # Fallback (still works, just more calls)
        for col, val in updates.items():
            wks.update_acell(f"{col}{row}", val)


# ---------------------------
# Topics generation
# ---------------------------

_SIMPLE_STOPWORDS = set("""
a an and are as at be but by for from has have he her hers him his i if in into is it its
me my of on or our ours she that the their theirs them they this to was we were what when
where which who why will with you your yours not
""".split())

def topics_simple(text: str, k: int = 8) -> List[str]:
    """
    Simple keyword-ish topics:
    - pull frequent capitalized phrases + frequent meaningful tokens
    - not perfect, but deterministic, fast, no extra API calls
    """
    if not text:
        return []

    # Capitalized phrases (good for entities/topics)
    caps = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b", text)
    freq: Dict[str, int] = {}
    for c in caps:
        c = c.strip()
        if len(c) < 4:
            continue
        freq[c] = freq.get(c, 0) + 1

    # Meaningful tokens
    tokens = re.findall(r"[A-Za-z][A-Za-z\-]{3,}", text.lower())
    for t in tokens:
        if t in _SIMPLE_STOPWORDS:
            continue
        freq[t] = freq.get(t, 0) + 1

    # Sort by frequency
    out = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)
    topics: List[str] = []
    for term, _ in out:
        # normalize: title-case simple tokens
        label = term if any(ch.isupper() for ch in term) else term.replace("-", " ").title()
        if label.lower() in _SIMPLE_STOPWORDS:
            continue
        if label in topics:
            continue
        topics.append(label)
        if len(topics) >= k:
            break

    return topics

def topics_openai(text: str, k: int = 8) -> List[str]:
    """
    Optional: higher-quality topics using OpenAI (if you want).
    Requires OPENAI_API_KEY in environment, which you already use elsewhere.
    """
    from openai import OpenAI
    from openai_compat import json_schema_response
    client = OpenAI()

    snippet = (text or "")[:8000]  # keep it small & cheap

    schema = {
        "name": "topics_schema",
        "schema": {
            "type": "object",
            "properties": {
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 0,
                    "maxItems": k,
                }
            },
            "required": ["topics"],
            "additionalProperties": False,
        },
        "strict": True,
    }

    sys = (
        "Extract concise topical tags from the text. "
        "Return 3–8 short topics (2–4 words), title case, no duplicates."
    )

    data, _raw = json_schema_response(
        client,
        model="gpt-4.1-mini",
        system_prompt=sys,
        user_content=snippet,
        json_schema=schema,
    )
    topics = [t.strip() for t in data.get("topics", []) if t and t.strip()]
    # dedupe
    uniq = []
    seen = set()
    for t in topics:
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(t)
    return uniq[:k]


# ---------------------------
# WordPress client call
# ---------------------------

def wp_headers() -> Dict[str, str]:
    user = os.getenv("WP_USERNAME", "")
    pw = os.getenv("WP_APP_PASSWORD", "")
    if not user or not pw:
        raise RuntimeError("Missing WP_USERNAME or WP_APP_PASSWORD in environment.")
    token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("utf-8")
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }

def wp_ingest(cfg: WPQueueConfig, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any], str]:
    url = cfg.wp_base_url.rstrip("/") + cfg.wp_ingest_path
    r = requests.post(url, headers=wp_headers(), json=payload, timeout=30)
    txt = r.text or ""
    try:
        js = r.json() if txt else {}
    except Exception:
        js = {}
    return r.status_code, js, txt[:4000]


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)

    gc = gs_client()
    wks = open_worksheet(gc, cfg.spreadsheet_url, cfg.worksheet_name)

    all_vals: List[List[str]] = wks.get_all_values()

    idx_xaio_status = col_letter_to_index(cfg.col_xaio_status)
    idx_xaio_path = col_letter_to_index(cfg.col_xaio_path)

    idx_wp_status = col_letter_to_index(cfg.col_wp_status)
    idx_wp_post_id = col_letter_to_index(cfg.col_wp_post_id)
    idx_wp_error = col_letter_to_index(cfg.col_wp_error)

    processed = 0

    for i, row in enumerate(all_vals[1:], start=2):  # 1-based + header row
        if processed >= cfg.max_per_run:
            break

        xaio_status = get_cell(row, idx_xaio_status)
        xaio_path_s = get_cell(row, idx_xaio_path)

        wp_status = get_cell(row, idx_wp_status)

        if xaio_status != "XAIO_DONE":
            continue
        if not xaio_path_s:
            continue
        if wp_status and wp_status not in ("WP_FAILED", ""):
            # Already running/done
            continue

        row_start = time.monotonic()

        xaio_path = Path(xaio_path_s).expanduser()
        item_id = xaio_path.stem.replace(".xaio_parsed", "")
        xaio = load_json(xaio_path)

        canonical_url = safe_str(xaio.get("canonical_url", "")).strip()
        content_mode = safe_str(xaio.get("content_mode", "url")).strip() or "url"
        domain = normalize_domain(safe_str(xaio.get("domain", "")))
        site_name = safe_str(xaio.get("site_name", "")).strip()

        # optional identity
        org_name = safe_str(xaio.get("organization_name") or site_name).strip()
        authors = xaio.get("author_names") if isinstance(xaio.get("author_names"), list) else []
        contributor_name = safe_str(authors[0]).strip() if authors else ""

        body_text = safe_str(xaio.get("extracted_text_full", "")).strip()

        # Topics
        topics: List[str] = []
        try:
            if cfg.topics_mode == "openai":
                topics = topics_openai(body_text, k=cfg.topics_max)
            else:
                topics = topics_simple(body_text, k=cfg.topics_max)
        except Exception as e:
            logger.warning(f"topic generation failed item={item_id} err={e}")
            topics = []

        # Build MU plugin payload
        payload: Dict[str, Any] = {
            "xaio_id": item_id,
            "canonical_url": canonical_url,
            "content_mode": content_mode,
            "content_title": safe_str(xaio.get("url_content_title") or xaio.get("title") or ""),
            "post_body": body_text,
            "contributor_name": contributor_name,
            "org_name": org_name,
            "org_domain": domain,
            "topics": topics,
        }

        # Mark WP_RUNNING
        safe_update_cells(wks, i, {
            cfg.col_wp_status: "WP_RUNNING",
            cfg.col_wp_post_id: "",
            cfg.col_wp_error: "",
        }, item_id=item_id, url=canonical_url)

        status_code, js, raw_txt = wp_ingest(cfg, payload)

        if status_code >= 300 or not js.get("ok"):
            err = f"HTTP {status_code} {raw_txt}"
            safe_update_cells(wks, i, {
                cfg.col_wp_status: "WP_FAILED",
                cfg.col_wp_post_id: "",
                cfg.col_wp_error: err[:49000],
            }, item_id=item_id, url=canonical_url)
            log_event(logger, stage="wp_failed", item_id=item_id, row=i, url=canonical_url,
                      elapsed_ms_value=elapsed_ms(row_start), message=err, level=logging.ERROR)
            processed += 1
            continue

        wp_post_id = js.get("content_id") or js.get("post_id") or ""
        safe_update_cells(wks, i, {
            cfg.col_wp_status: "WP_DONE",
            cfg.col_wp_post_id: safe_str(wp_post_id),
            cfg.col_wp_error: "",
        }, item_id=item_id, url=canonical_url)

        log_event(logger, stage="wp_done", item_id=item_id, row=i, url=canonical_url,
                  elapsed_ms_value=elapsed_ms(row_start), message=f"post_id={wp_post_id}")

        processed += 1

    log_event(logger, stage="run_complete", message=f"processed={processed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
