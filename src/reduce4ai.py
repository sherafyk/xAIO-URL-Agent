#!/usr/bin/env python3
"""
reduce4ai.py

Reads a "capture JSON" produced by your existing pipeline and writes a smaller,
AI-ready envelope JSON ("ai_input.json") that:

- keeps full extracted text (verbatim) for context
- keeps a curated meta whitelist (title/description/site/published hints)
- drops large/noisy fields (raw_html, meta dumps, etc.) from the AI payload
- adds cheap computed stats (char/word count, sha256) for dedupe/traceability
- leaves the original capture JSON unchanged

Usage:
  python reduce4ai.py /path/to/capture.json
  python reduce4ai.py /path/to/capture.json --outdir ./out_ai --prompt-set-id xaio-v1

"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from logging_utils import elapsed_ms, log_event, setup_logging

logger = setup_logging("reduce4ai")

# ---------------------------
# helpers
# ---------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def safe_str(x: Any) -> str:
    return (x if isinstance(x, str) else "" if x is None else str(x)).strip()

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def word_count(text: str) -> int:
    # simple and fast; good enough for stats + budgeting
    return len([w for w in text.split() if w])

def clean_url(url: str, drop_params: Tuple[str, ...] = ("utm_source","utm_medium","utm_campaign","utm_term","utm_content","utm_id","utm_name","utm_reader","utm_referrer","utm_social","fbclid","gclid")) -> str:
    """
    Canonical-ish cleanup:
      - removes fragments
      - removes common tracking parameters
      - keeps other query params
    """
    url = safe_str(url)
    if not url:
        return ""
    try:
        u = urlparse(url)
        q = [(k, v) for (k, v) in parse_qsl(u.query, keep_blank_values=True) if k not in drop_params]
        new_query = urlencode(q, doseq=True)
        u2 = u._replace(query=new_query, fragment="")
        return urlunparse(u2)
    except Exception:
        return url

def get_nested(d: Dict[str, Any], path: str) -> Any:
    """
    Safe getter for nested dict paths like "url.final" or "page.meta.og:title"
    """
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur

def first_nonempty(*vals: Any) -> str:
    for v in vals:
        s = safe_str(v)
        if s:
            return s
    return ""

def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        # strip credentials if any
        if "@" in host:
            host = host.split("@", 1)[1]
        # strip port
        if ":" in host:
            host = host.split(":", 1)[0]
        return host
    except Exception:
        return ""

def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def unique_strings(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            val = item.strip()
            if val and val not in seen:
                out.append(val)
                seen.add(val)
    return out

# ---------------------------
# core extraction from capture JSON
# ---------------------------

@dataclass
class CaptureSignals:
    original_url: str
    final_url: str
    canonical_hint: str
    title: str
    description: str
    site_name: str
    published_at_hint: str
    author_hint: str
    extracted_text: str
    fetch_method: str

def extract_signals(capture: Dict[str, Any]) -> CaptureSignals:
    """
    Be robust across versions: try multiple likely paths for each signal.
    """

    # URLs
    original_url = first_nonempty(
        get_nested(capture, "url.original"),
        get_nested(capture, "urls.original"),
        get_nested(capture, "payload.url.original"),
    )
    final_url = first_nonempty(
        get_nested(capture, "url.final"),
        get_nested(capture, "urls.final"),
        get_nested(capture, "fetch.final_url"),
        get_nested(capture, "page.url"),
        original_url,
    )
    canonical_hint = first_nonempty(
        get_nested(capture, "url.canonical"),
        get_nested(capture, "urls.canonical"),
        get_nested(capture, "page.canonical_url"),
        get_nested(capture, "page.meta.canonical"),
        get_nested(capture, "page.meta.og:url"),
        final_url,
        original_url,
    )

    # Page/meta
    title = first_nonempty(
        get_nested(capture, "page.title"),
        get_nested(capture, "page.meta.og:title"),
        get_nested(capture, "page.meta.twitter:title"),
        get_nested(capture, "title"),
    )
    description = first_nonempty(
        get_nested(capture, "page.description"),
        get_nested(capture, "page.meta.og:description"),
        get_nested(capture, "page.meta.description"),
        get_nested(capture, "description"),
    )
    site_name = first_nonempty(
        get_nested(capture, "page.site_name"),
        get_nested(capture, "page.meta.og:site_name"),
        get_nested(capture, "site.name"),
    )
    published_at_hint = first_nonempty(
        get_nested(capture, "page.published_at"),
        get_nested(capture, "page.meta.article:published_time"),
        get_nested(capture, "page.meta.og:published_time"),
        get_nested(capture, "page.meta.parsely-pub-date"),
        get_nested(capture, "published_at"),
    )
    author_hint = first_nonempty(
        get_nested(capture, "page.byline"),
        get_nested(capture, "page.author"),
        get_nested(capture, "page.meta.author"),
        get_nested(capture, "page.meta.article:author"),
        get_nested(capture, "author"),
    )

    # Content text (you said you want ALL of it, so we keep verbatim)
    extracted_text = first_nonempty(
        get_nested(capture, "content.text"),
        get_nested(capture, "content.extracted_text"),
        get_nested(capture, "extracted_text"),
        get_nested(capture, "payload.extracted_text"),
    )

    fetch_method = first_nonempty(
        get_nested(capture, "fetch.method"),
        get_nested(capture, "payload.fetch.method"),
        get_nested(capture, "method"),
    )

    return CaptureSignals(
        original_url=safe_str(original_url),
        final_url=safe_str(final_url),
        canonical_hint=safe_str(canonical_hint),
        title=safe_str(title),
        description=safe_str(description),
        site_name=safe_str(site_name),
        published_at_hint=safe_str(published_at_hint),
        author_hint=safe_str(author_hint),
        extracted_text=safe_str(extracted_text),
        fetch_method=safe_str(fetch_method),
    )


# ---------------------------
# build AI envelope
# ---------------------------

def build_ai_envelope(
    capture_path: Path,
    capture: Dict[str, Any],
    prompt_set_id: str,
    include_meta_keys: Tuple[str, ...],
) -> Dict[str, Any]:
    sig = extract_signals(capture)

    # Clean URLs for the AI (reduces noise like UTM)
    original_clean = clean_url(sig.original_url)
    final_clean = clean_url(sig.final_url)
    canonical_clean = clean_url(sig.canonical_hint)

    text = sig.extracted_text
    text_sha = sha256_hex(text) if text else ""
    chars = len(text)
    words = word_count(text) if text else 0

    # Whitelist a *small* set of meta keys if present (optional).
    # This avoids dumping thousands of meta entries into the model.
    meta_whitelist: Dict[str, str] = {}
    meta_dict = get_nested(capture, "page.meta")
    if isinstance(meta_dict, dict) and include_meta_keys:
        for k in include_meta_keys:
            v = meta_dict.get(k)
            if isinstance(v, str) and v.strip():
                meta_whitelist[k] = v.strip()

    jsonld_extracted = get_nested(capture, "page.jsonld_extracted") or {}
    identity_candidates = {
        "organization_names": unique_strings(jsonld_extracted.get("publisher_names")),
        "author_names": unique_strings(jsonld_extracted.get("author_names")),
        "date_published": unique_strings(jsonld_extracted.get("date_published")),
        "date_modified": unique_strings(jsonld_extracted.get("date_modified")),
    }

    # Create a stable ID for traceability
    capture_id = f"{capture_path.name}__textsha:{text_sha[:12] if text_sha else 'no_text'}"

    envelope = {
        "xaio_parse_request_version": "1.0",
        "capture": {
            "capture_id": capture_id,
            "source_capture_json_path": str(capture_path),
            "collected_at_utc": first_nonempty(get_nested(capture, "ingest.seen_at"), get_nested(capture, "submitted_at"), now_iso()),
        },
        "url": {
            "original": sig.original_url,
            "final": sig.final_url,
            "canonical_hint": sig.canonical_hint,
            "clean": {
                "original": original_clean,
                "final": final_clean,
                "canonical": canonical_clean,
            },
            "domain": domain_of(final_clean or canonical_clean or original_clean),
        },
        "meta": {
            "title": sig.title,
            "description": sig.description,
            "site_name": sig.site_name,
            "published_at_hint": sig.published_at_hint,
            "author_hint": sig.author_hint,
            "fetch_method": sig.fetch_method,
            "meta_whitelist": meta_whitelist,  # small optional subset
            "identity_candidates": identity_candidates,
        },
        "content": {
            # FULL TEXT (verbatim) — per your requirement
            "extracted_text_full": text,
            "char_count": chars,
            "word_count": words,
            "sha256": text_sha,
        },
        "instructions": {
            "target_cpt": "content",
            "prompt_set_id": prompt_set_id,
            "claims_granularity": "atomic",
            "claims_should_be_verifiable": True,
            "no_fact_checking_yet": True,
            "verdict_default": "unverified",
        },
    }

    return envelope


# ---------------------------
# CLI
# ---------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Reduce capture JSON into AI-ready xAIO envelope JSON.")
    ap.add_argument("capture_json", help="Path to the capture JSON produced by your current pipeline.")
    ap.add_argument("--outdir", default="./out_ai", help="Directory to write AI envelope JSON files into. Default: ./out_ai")
    ap.add_argument("--prompt-set-id", default="xaio-v1-claims+scores", help="Identifier recorded in the envelope for traceability.")
    ap.add_argument(
        "--meta-keys",
        default="og:title,og:description,og:site_name,og:url,article:published_time,article:modified_time,article:section,article:tag,twitter:title,twitter:description,description,author",
        help="Comma-separated meta keys to whitelist (from capture.page.meta). Default includes common OG/article/twitter keys.",
    )
    args = ap.parse_args()

    capture_path = Path(args.capture_json).expanduser().resolve()
    item_id = capture_path.stem
    start_time = time.monotonic()
    log_event(logger, stage="reduce_start", item_id=item_id, message="reduce starting")

    try:
        if not capture_path.exists():
            raise FileNotFoundError(f"Capture JSON not found: {capture_path}")

        outdir = Path(args.outdir).expanduser().resolve()
        ensure_dir(outdir)

        include_meta_keys = tuple([k.strip() for k in args.meta_keys.split(",") if k.strip()])

        with capture_path.open("r", encoding="utf-8") as f:
            capture = json.load(f)

        envelope = build_ai_envelope(
            capture_path=capture_path,
            capture=capture,
            prompt_set_id=args.prompt_set_id,
            include_meta_keys=include_meta_keys,
        )

        # Write output next to outdir; name derived from capture name
        out_name = capture_path.stem + ".ai_input.json"
        out_path = outdir / out_name
        out_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")

        log_event(
            logger,
            stage="reduce_done",
            item_id=item_id,
            elapsed_ms_value=elapsed_ms(start_time),
            message=f"wrote={out_path} chars={envelope['content']['char_count']} words={envelope['content']['word_count']}",
            domain=envelope["url"]["domain"],
        )
        return 0
    except Exception as exc:
        log_event(
            logger,
            stage="reduce_failed",
            item_id=item_id,
            elapsed_ms_value=elapsed_ms(start_time),
            message=f"{type(exc).__name__}: {exc}",
            level=logging.ERROR,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
