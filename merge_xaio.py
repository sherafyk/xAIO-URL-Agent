#!/usr/bin/env python3
"""
merge_xaio.py
Merges meta_parsed.json + claims_parsed.json + ai_input.json into final *.xaio_parsed.json
"""

import argparse, json
from pathlib import Path
from typing import Any, Dict

def load_json(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ai_input_json")
    ap.add_argument("meta_parsed_json")
    ap.add_argument("claims_parsed_json")
    ap.add_argument("--outdir", default="./out_xaio")
    args = ap.parse_args()

    ai_path = Path(args.ai_input_json).expanduser().resolve()
    meta_path = Path(args.meta_parsed_json).expanduser().resolve()
    claims_path = Path(args.claims_parsed_json).expanduser().resolve()
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    ai_input: Dict[str, Any] = load_json(ai_path)
    meta: Dict[str, Any] = load_json(meta_path)
    claims: Dict[str, Any] = load_json(claims_path)

    out: Dict[str, Any] = {}
    out.update(meta)
    out["claims"] = claims.get("claims", [])

    # Always inject verbatim full text + counts from capture
    content = ai_input.get("content", {}) if isinstance(ai_input.get("content"), dict) else {}
    out["extracted_text_full"] = content.get("extracted_text_full", "")

    # If you store these in your content block, carry them through
    if "char_count" in content:
        out["char_count"] = content.get("char_count")
    if "word_count" in content:
        out["word_count"] = content.get("word_count")

    base = ai_path.stem.replace(".ai_input", "")
    out_path = outdir / f"{base}.xaio_parsed.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote: {out_path}")
    print(f"Claims: {len(out.get('claims', []))}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

