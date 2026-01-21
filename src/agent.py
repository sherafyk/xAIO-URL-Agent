import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import gspread
import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
from readability import Document
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from logging_utils import elapsed_ms, log_event, setup_logging

logger = setup_logging("agent")


# ---------- utils ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def col_letter_to_index(letter: str) -> int:
    letter = letter.strip().upper()
    n = 0
    for ch in letter:
        n = n * 26 + (ord(ch) - ord('A') + 1)
    return n

def sha12(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]

def safe_str(x: Any) -> str:
    return (x or "").strip()

def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


# ---------- config ----------

@dataclass
class Config:
    spreadsheet_url: str
    worksheet_name: str
    header_row: int
    first_data_row: int

    col_url: str
    col_status: str
    col_processed_at: str
    col_final_url: str
    col_method: str
    col_json_path: str
    col_title: str
    col_error: str

    output_dir: Path
    sqlite_path: Path
    max_per_run: int

    try_http_first: bool
    http_timeout_s: int
    browser_cdp_endpoint: str
    browser_nav_timeout_ms: int
    browser_wait_after_load_ms: int


def load_config(path: str = "config.yaml") -> Config:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return Config(
        spreadsheet_url=data["sheet"]["spreadsheet_url"],
        worksheet_name=data["sheet"]["worksheet_name"],
        header_row=int(data["sheet"].get("header_row", 1)),
        first_data_row=int(data["sheet"].get("first_data_row", 2)),

        col_url=data["columns"]["url"],
        col_status=data["columns"]["status"],
        col_processed_at=data["columns"]["processed_at"],
        col_final_url=data["columns"]["final_url"],
        col_method=data["columns"]["method"],
        col_json_path=data["columns"]["json_path"],
        col_title=data["columns"]["title"],
        col_error=data["columns"]["error"],

        output_dir=Path(data["agent"]["output_dir"]),
        sqlite_path=Path(data["agent"]["sqlite_path"]),
        max_per_run=int(data["agent"].get("max_per_run", 50)),

        try_http_first=bool(data["fetch"].get("try_http_first", True)),
        http_timeout_s=int(data["fetch"].get("http_timeout_s", 20)),
        browser_cdp_endpoint=data["fetch"]["browser_cdp_endpoint"],
        browser_nav_timeout_ms=int(data["fetch"].get("browser_nav_timeout_ms", 30000)),
        browser_wait_after_load_ms=int(data["fetch"].get("browser_wait_after_load_ms", 1500)),
    )


# ---------- sqlite state (idempotency + history) ----------

def db_init(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url_original TEXT NOT NULL,
            url_final TEXT,
            url_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            processed_at TEXT,
            method TEXT,
            json_path TEXT,
            error TEXT
        );
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_hash ON items(url_hash);")
    conn.commit()
    return conn

def db_seen(conn: sqlite3.Connection, url_hash: str) -> Optional[Dict[str, Any]]:
    cur = conn.execute("SELECT status, json_path, processed_at FROM items WHERE url_hash = ?", (url_hash,))
    row = cur.fetchone()
    if not row:
        return None
    return {"status": row[0], "json_path": row[1], "processed_at": row[2]}

def db_upsert_start(conn: sqlite3.Connection, url_original: str, url_hash: str) -> None:
    conn.execute("""
        INSERT INTO items(url_original, url_hash, status, created_at)
        VALUES(?,?,?,?)
        ON CONFLICT(url_hash) DO UPDATE SET
            url_original=excluded.url_original
    """, (url_original, url_hash, "FETCHING", now_iso()))
    conn.commit()

def db_finish(conn: sqlite3.Connection, url_hash: str, *, status: str, url_final: str = "",
              method: str = "", json_path: str = "", error: str = "") -> None:
    conn.execute("""
        UPDATE items
        SET status=?, processed_at=?, url_final=?, method=?, json_path=?, error=?
        WHERE url_hash=?
    """, (status, now_iso(), url_final, method, json_path, error, url_hash))
    conn.commit()


# ---------- extraction ----------

def extract_meta_and_text(full_html: str, final_url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(full_html, "lxml")
    head = soup.head
    title_tag = soup.title.string.strip() if soup.title and soup.title.string else ""

    meta: Dict[str, str] = {}
    if head:
        for m in head.find_all("meta"):
            k = m.get("property") or m.get("name")
            v = m.get("content")
            if k and v:
                meta[k.strip()] = v.strip()

    canonical = ""
    if head:
        link = head.find("link", rel=lambda x: x and "canonical" in x.lower())
        if link and link.get("href"):
            canonical = link["href"].strip()

    jsonld_extracted = extract_jsonld_candidates(soup)

    # readability main content
    doc = Document(full_html)
    readable_title = safe_str(doc.short_title())
    main_html = doc.summary(html_partial=True)
    main_text = BeautifulSoup(main_html, "lxml").get_text("\n", strip=True)

    # best-effort published time
    published_raw = (
        meta.get("article:published_time")
        or meta.get("og:published_time")
        or meta.get("pubdate")
        or meta.get("date")
    )
    published_at = ""
    if published_raw:
        try:
            published_at = dateparser.parse(published_raw).astimezone(timezone.utc).replace(microsecond=0).isoformat()
        except Exception:
            published_at = published_raw

    site_name = meta.get("og:site_name", "")
    og_title = meta.get("og:title", "")
    description = meta.get("og:description") or meta.get("description") or ""

    return {
        "url": {"final": final_url, "canonical": canonical or final_url},
        "page": {
            "title": title_tag or og_title or readable_title,
            "site_name": site_name,
            "description": description,
            "published_at": published_at,
            "meta": meta,
            "jsonld_extracted": jsonld_extracted,
        },
        "content": {
            "text": main_text,
        }
    }


def extract_jsonld_candidates(soup: BeautifulSoup) -> Dict[str, List[str]]:
    scripts = soup.find_all("script", attrs={"type": lambda x: isinstance(x, str) and "ld+json" in x.lower()})
    parsed_items: List[Any] = []
    for script in scripts:
        if not script.string:
            continue
        raw = script.string.strip()
        if not raw:
            continue
        try:
            parsed_items.append(json.loads(raw))
        except json.JSONDecodeError:
            continue

    org_names: List[str] = []
    author_names: List[str] = []
    date_published: List[str] = []
    date_modified: List[str] = []

    org_seen: set[str] = set()
    author_seen: set[str] = set()
    published_seen: set[str] = set()
    modified_seen: set[str] = set()

    def add_name(value: Any, target: List[str], seen: set[str]) -> None:
        if isinstance(value, list):
            for item in value:
                add_name(item, target, seen)
            return
        if isinstance(value, dict):
            name = value.get("name") or value.get("legalName")
            if isinstance(name, str):
                add_name(name, target, seen)
            return
        if isinstance(value, str):
            name = value.strip()
            if name and name not in seen:
                target.append(name)
                seen.add(name)

    def add_date(value: Any, target: List[str], seen: set[str]) -> None:
        if isinstance(value, list):
            for item in value:
                add_date(item, target, seen)
            return
        if isinstance(value, str):
            date_str = value.strip()
            if date_str and date_str not in seen:
                target.append(date_str)
                seen.add(date_str)

    def iter_nodes(obj: Any) -> Iterable[Dict[str, Any]]:
        if isinstance(obj, dict):
            yield obj
            graph = obj.get("@graph")
            if graph is not None:
                yield from iter_nodes(graph)
            for val in obj.values():
                yield from iter_nodes(val)
        elif isinstance(obj, list):
            for item in obj:
                yield from iter_nodes(item)

    for item in parsed_items:
        for node in iter_nodes(item):
            if "publisher" in node:
                add_name(node.get("publisher"), org_names, org_seen)
            if "author" in node:
                add_name(node.get("author"), author_names, author_seen)
            if "datePublished" in node:
                add_date(node.get("datePublished"), date_published, published_seen)
            if "dateModified" in node:
                add_date(node.get("dateModified"), date_modified, modified_seen)

    return {
        "publisher_names": org_names,
        "author_names": author_names,
        "date_published": date_published,
        "date_modified": date_modified,
    }


# ---------- fetching ----------

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

@retry(
    reraise=True,
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=6),
    retry=retry_if_exception_type((requests.RequestException,)),
)
def fetch_http(url: str, timeout_s: int) -> Tuple[str, str]:
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.9"},
        timeout=timeout_s,
        allow_redirects=True,
    )
    ct = resp.headers.get("content-type", "")
    if resp.status_code >= 400:
        raise requests.RequestException(f"HTTP {resp.status_code}")
    if "text/html" not in ct and "application/xhtml+xml" not in ct:
        raise requests.RequestException(f"Non-HTML content-type: {ct}")
    return resp.text, resp.url

def fetch_browser(url: str, cfg: Config, browser_holder: Dict[str, Any]) -> Tuple[str, str]:
    # Connect once per run, reuse.
    if "browser" not in browser_holder:
        pw = browser_holder["pw"]  # set by caller
        browser_holder["browser"] = pw.chromium.connect_over_cdp(cfg.browser_cdp_endpoint)
        # Per Playwright docs, the default context is accessible via browser.contexts[0]. :contentReference[oaicite:5]{index=5}
        browser_holder["context"] = (
            browser_holder["browser"].contexts[0]
            if browser_holder["browser"].contexts
            else browser_holder["browser"].new_context()
        )

    context = browser_holder["context"]
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=cfg.browser_nav_timeout_ms)
        # give JS-heavy pages a moment to render
        if cfg.browser_wait_after_load_ms > 0:
            page.wait_for_timeout(cfg.browser_wait_after_load_ms)
        full_html = page.content()
        final_url = page.url
        return full_html, final_url
    finally:
        page.close()


# ---------- sheets ----------

def gs_client() -> gspread.Client:
    # expects secrets/service_account.json by default
    return gspread.service_account(filename="secrets/service_account.json")

def open_worksheet(gc: gspread.Client, spreadsheet_url: str, worksheet_name: str):
    sh = gc.open_by_url(spreadsheet_url)
    return sh.worksheet(worksheet_name)

def cell_addr(col_letter: str, row: int) -> str:
    return f"{col_letter}{row}"

def update_row(wks, row: int, updates: Dict[str, str]) -> None:
    # updates: { "B": "DONE", "C": "...", ... }
    # Do a single range update if contiguous; for simplicity we update cell-by-cell (low volume).
    for col, val in updates.items():
        wks.update_acell(cell_addr(col, row), val)


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(Exception),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def update_row_with_retry(wks, row: int, updates: Dict[str, str]) -> None:
    update_row(wks, row, updates)


def safe_update_row(wks, row: int, updates: Dict[str, str], *, item_id: str, url: str) -> None:
    try:
        update_row_with_retry(wks, row, updates)
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


# ---------- main loop (one timer run) ----------

def run_once(cfg: Config) -> None:
    ensure_dir(cfg.output_dir)
    conn = db_init(cfg.sqlite_path)

    gc = gs_client()
    wks = open_worksheet(gc, cfg.spreadsheet_url, cfg.worksheet_name)

    # Pull all values (fine for a few thousand rows; simplest v1).
    all_vals: List[List[str]] = wks.get_all_values()

    url_i = col_letter_to_index(cfg.col_url) - 1
    status_i = col_letter_to_index(cfg.col_status) - 1

    processed = 0
    browser_holder: Dict[str, Any] = {}

    log_event(logger, stage="run_start", message="capture run starting")

    with sync_playwright() as pw:
        browser_holder["pw"] = pw

        for sheet_row_idx in range(cfg.first_data_row, len(all_vals) + 1):
            row_vals = all_vals[sheet_row_idx - 1]

            url = safe_str(row_vals[url_i] if url_i < len(row_vals) else "")
            status = safe_str(row_vals[status_i] if status_i < len(row_vals) else "").upper()

            if not url:
                continue
            if status in ("DONE", "FETCHING"):
                continue

            # stop if batch limit reached
            if processed >= cfg.max_per_run:
                break

            uhash = sha12(url)
            seen = db_seen(conn, uhash)
            if seen and seen["status"] == "DONE":
                # keep sheet consistent if someone cleared status
                safe_update_row(wks, sheet_row_idx, {
                    cfg.col_status: "DONE",
                    cfg.col_processed_at: seen.get("processed_at") or "",
                    cfg.col_json_path: seen.get("json_path") or "",
                }, item_id=uhash, url=url)
                continue

            row_start = time.monotonic()
            log_event(logger, stage="row_start", item_id=uhash, row=sheet_row_idx, url=url)

            # claim
            safe_update_row(wks, sheet_row_idx, {
                cfg.col_status: "FETCHING",
                cfg.col_processed_at: now_iso(),
                cfg.col_error: "",
            }, item_id=uhash, url=url)
            db_upsert_start(conn, url, uhash)

            try:
                method = ""
                final_url = url
                html = ""

                if cfg.try_http_first:
                    try:
                        html, final_url = fetch_http(url, cfg.http_timeout_s)
                        method = "http"
                    except Exception:
                        html, final_url = fetch_browser(url, cfg, browser_holder)
                        method = "browser"
                else:
                    html, final_url = fetch_browser(url, cfg, browser_holder)
                    method = "browser"

                extracted = extract_meta_and_text(html, final_url)

                # write JSON
                day_dir = cfg.output_dir / datetime.now().strftime("%Y/%m/%d")
                ensure_dir(day_dir)
                out_path = day_dir / f"{uhash}.json"

                payload = {
                    "schema_version": "1.0",
                    "ingest": {
                        "source": "google_sheets",
                        "spreadsheet_url": cfg.spreadsheet_url,
                        "worksheet": cfg.worksheet_name,
                        "row": sheet_row_idx,
                        "seen_at": now_iso(),
                    },
                    "url": {
                        "original": url,
                        "final": final_url,
                        "canonical": extracted["url"]["canonical"],
                    },
                    "fetch": {
                        "method": method,
                        "fetched_at": now_iso(),
                    },
                    "page": extracted["page"],
                    "content": {
                        "text": extracted["content"]["text"],
                        # optional: keep raw HTML for reprocessing
                        "raw_html": html,
                    },
                }

                out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

                title = safe_str(payload["page"].get("title", ""))

                db_finish(conn, uhash, status="DONE", url_final=final_url, method=method, json_path=str(out_path))
                safe_update_row(wks, sheet_row_idx, {
                    cfg.col_status: "DONE",
                    cfg.col_processed_at: now_iso(),
                    cfg.col_final_url: final_url,
                    cfg.col_method: method,
                    cfg.col_json_path: str(out_path),
                    cfg.col_title: title,
                    cfg.col_error: "",
                }, item_id=uhash, url=final_url)

                processed += 1
                log_event(
                    logger,
                    stage="row_done",
                    item_id=uhash,
                    row=sheet_row_idx,
                    url=final_url,
                    elapsed_ms_value=elapsed_ms(row_start),
                    message=f"method={method}",
                )

            except (PWTimeoutError, Exception) as e:
                err = f"{type(e).__name__}: {str(e)}"
                db_finish(conn, uhash, status="FAILED", error=err)
                safe_update_row(wks, sheet_row_idx, {
                    cfg.col_status: "FAILED",
                    cfg.col_processed_at: now_iso(),
                    cfg.col_error: err[:49000],  # keep under cell limits
                }, item_id=uhash, url=url)
                log_event(
                    logger,
                    stage="row_failed",
                    item_id=uhash,
                    row=sheet_row_idx,
                    url=url,
                    elapsed_ms_value=elapsed_ms(row_start),
                    message=err,
                    level=logging.ERROR,
                )

        # Disconnect cleanly: for connected browsers, Playwright documents that browser.close()
        # “disconnects from the browser server” (vs killing the browser). :contentReference[oaicite:6]{index=6}
        if "browser" in browser_holder:
            try:
                browser_holder["browser"].close()
            except Exception:
                pass

    log_event(logger, stage="run_complete", message=f"processed={processed}")


if __name__ == "__main__":
    cfg = load_config("config.yaml")
    run_once(cfg)
    log_event(logger, stage="exit", message="run complete")
