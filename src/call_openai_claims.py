#!/usr/bin/env python3
"""
call_openai_claims.py
Reads *.ai_input.json (with extracted_text_full) + meta_parsed.json and outputs claims only.
"""

from __future__ import annotations
import argparse, json, re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

from openai import OpenAI
from pydantic import BaseModel, Field

def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def find_select_choices(scf_export: Any, field_name: str) -> Dict[str, str]:
    if not isinstance(scf_export, list):
        return {}
    for group in scf_export:
        fields = group.get("fields", []) if isinstance(group, dict) else []
        for f in fields:
            if not isinstance(f, dict):
                continue
            if f.get("name") == field_name and f.get("type") == "select":
                return f.get("choices", {}) or {}
            if f.get("type") in ("repeater", "group"):
                for sf in f.get("sub_fields", []) or []:
                    if isinstance(sf, dict) and sf.get("name") == field_name and sf.get("type") == "select":
                        return sf.get("choices", {}) or {}
    return {}

def scf_claim_type_choices(scf_export_path: Path) -> List[str]:
    scf = load_json(scf_export_path)
    keys = list((find_select_choices(scf, "claim_type") or {}).keys())
    return sorted([k for k in keys if isinstance(k, str) and k.strip() != ""])

def normalize_claim_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def build_model(claim_types: List[str]) -> Type[BaseModel]:
    class Claim(BaseModel):
        claim_text: str = Field(..., description="One atomic, checkable statement from the content.")
        claim_type: str = Field(..., description=f"One of: {claim_types}")

    class ClaimsParsed(BaseModel):
        canonical_url: str
        claims: List[Claim] = Field(default_factory=list)

    return ClaimsParsed

SYSTEM_PROMPT = """You are xAIO Claims Extractor.

Return ONLY valid JSON matching the provided schema (strict).
Do not include markdown, commentary, or extra keys.

Rules:
- Extract claims ONLY from extracted_text_full.
- One claim per atomic, checkable statement.
- Do NOT fact-check. Do NOT output verdict/confidence/severity/source/notes.
- Output ONLY claim_text and claim_type per claim.
"""

def call_openai_structured(model: str, schema: Type[BaseModel], user_input: Dict[str, Any], reasoning_effort: Optional[str]) -> Tuple[Optional[BaseModel], Dict[str, Any]]:
    client = OpenAI()
    req: Dict[str, Any] = dict(
        model=model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_input, ensure_ascii=False)},
        ],
        text_format=schema,
    )
    if reasoning_effort:
        req["reasoning"] = {"effort": reasoning_effort}
    resp = client.responses.parse(**req)  # type: ignore
    raw = resp.model_dump() if hasattr(resp, "model_dump") else json.loads(resp.json())
    parsed = getattr(resp, "output_parsed", None)
    return parsed, raw

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ai_input_json")
    ap.add_argument("meta_parsed_json")
    ap.add_argument("--scf-export", default="./scf-export-content.json")
    ap.add_argument("--outdir", default="./out_claims")
    ap.add_argument("--write-raw", action="store_true")
    ap.add_argument("--model", default="gpt-5-nano")
    ap.add_argument("--reasoning-effort", default="minimal")
    args = ap.parse_args()

    ai_path = Path(args.ai_input_json).expanduser().resolve()
    meta_path = Path(args.meta_parsed_json).expanduser().resolve()
    scf_path = Path(args.scf_export).expanduser().resolve()
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    ai_input = load_json(ai_path)

    canon = (((ai_input.get("url") or {}).get("clean") or {}).get("canonical")
             or (ai_input.get("url") or {}).get("final")
             or (ai_input.get("url") or {}).get("original")

    user_input = {
      "canonical_url": canon,
      "meta": ai_input.get("meta", {}),
      "content": {
         "extracted_text_full": (ai_input.get("content") or {}).get("extracted_text_full","")
      }
    }


    claim_types = scf_claim_type_choices(scf_path)
    Schema = build_model(claim_types)

    parsed, raw = call_openai_structured(args.model, Schema, claims_input, args.reasoning_effort)
    if parsed is None:
        raw_path = outdir / (ai_path.stem.replace(".ai_input", "") + ".claims_response_raw.json")
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        raise RuntimeError(f"Parse failure/refusal. Raw: {raw_path}")

    out = parsed.model_dump()

    # Normalize claim text
    cleaned = []
    for c in out.get("claims", []) or []:
        ct = normalize_claim_text(c.get("claim_text", ""))
        if ct:
            c["claim_text"] = ct
            cleaned.append(c)
    out["claims"] = cleaned

    base = ai_path.stem.replace(".ai_input", "")
    out_path = outdir / f"{base}.claims_parsed.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.write_raw:
        raw_path = outdir / f"{base}.claims_response_raw.json"
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Wrote: {out_path}")
    print(f"Claims: {len(out.get('claims', []))}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

