#!/usr/bin/env python3
"""
call_openai_meta.py
Calls OpenAI Structured Outputs to parse META fields using ai_input without body text.
"""

from __future__ import annotations
import argparse, json
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

def scf_enums_from_export(scf_export_path: Path) -> Dict[str, List[str]]:
    scf = load_json(scf_export_path)
    def keys(name: str) -> List[str]:
        k = list((find_select_choices(scf, name) or {}).keys())
        # filter weird empty option
        return sorted([x for x in k if isinstance(x, str) and x.strip() != ""])
    return {
        "content_mode": keys("content_mode"),
        "language": keys("language"),
        "workflow_status": keys("workflow_status"),
        "intake_kind": keys("intake_kind"),
    }

def build_model(enums: Dict[str, List[str]]) -> Type[BaseModel]:
    class MetaParsed(BaseModel):
        canonical_url: str

        # SCF fields you said you want filled
        domain: Optional[str] = None
        site_name: Optional[str] = None
        published_at: Optional[str] = Field(None, description="ISO 8601 when available, else null.")
        modified_time: Optional[str] = Field(None, description="ISO 8601 when available, else null.")
        collected_at_utc: Optional[str] = Field(None, description="ISO 8601 when available, else null.")

        # Taxonomy fields (enums)
        content_mode: str = Field(..., description=f"Choose from: {enums['content_mode']}")
        language: Optional[str] = Field(None, description=f"Choose from: {enums['language']} or null.")
        workflow_status: Optional[str] = Field(None, description=f"Choose from: {enums['workflow_status']} or null.")
        intake_kind: Optional[str] = Field(None, description=f"Choose from: {enums['intake_kind']} or null.")
    return MetaParsed

SYSTEM_PROMPT = """You are xAIO Metadata Extractor.

Return ONLY valid JSON matching the provided schema (strict).
Do not include markdown, commentary, or extra keys.

Rules:
- Do NOT analyze or infer from any full article body (it is intentionally absent).
- Use only the provided URL + HEAD/metadata fields in the input JSON.
- If a value is not present in metadata, return null.
- content_mode must be one of the allowed values.
- language must be one of the allowed values or null.
"""

def call_openai_structured(model: str, schema: Type[BaseModel], meta_input: Dict[str, Any], reasoning_effort: Optional[str]) -> Tuple[Optional[BaseModel], Dict[str, Any]]:
    client = OpenAI()
    req: Dict[str, Any] = dict(
        model=model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(meta_input, ensure_ascii=False)},
        ],
        text_format=schema,
    )
    if reasoning_effort:
        req["reasoning"] = {"effort": reasoning_effort}
    resp = client.responses.parse(**req)  # type: ignore
    raw = resp.model_dump() if hasattr(resp, "model_dump") else json.loads(resp.json())
    parsed = getattr(resp, "output_parsed", None)
    return parsed, raw

def postprocess(out: Dict[str, Any], meta_input: Dict[str, Any]) -> Dict[str, Any]:
    # Canonical URL: always from the clean canonical if available
    canon = (((meta_input.get("url") or {}).get("clean") or {}).get("canonical")
             or (meta_input.get("url") or {}).get("canonical_hint")
             or (meta_input.get("url") or {}).get("final")
             or (meta_input.get("url") or {}).get("original"))
    if canon:
        out["canonical_url"] = canon

    # Deterministic fields
    url = meta_input.get("url") or {}
    meta = meta_input.get("meta") or {}
    mw = meta.get("meta_whitelist") or {}
    cap = meta_input.get("capture") or {}
    content = meta_input.get("content") or {}

    out["domain"] = url.get("domain") or out.get("domain")
    out["site_name"] = meta.get("site_name") or out.get("site_name")
    out["collected_at_utc"] = cap.get("collected_at_utc") or out.get("collected_at_utc")

    # Timestamps (prefer schema-ish meta_whitelist)
    out["published_at"] = mw.get("article:published_time") or meta.get("published_at_hint") or out.get("published_at")
    out["modified_time"] = mw.get("article:modified_time") or out.get("modified_time")

    # Counts (cheap + stable)
    out["char_count"] = content.get("char_count") or out.get("char_count")
    out["word_count"] = content.get("word_count") or out.get("word_count")

    return out

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("meta_input_json")
    ap.add_argument("--scf-export", default="./scf-export-content.json")
    ap.add_argument("--outdir", default="./out_meta")
    ap.add_argument("--write-raw", action="store_true")
    ap.add_argument("--model", default="gpt-5-nano")
    ap.add_argument("--reasoning-effort", default="minimal")
    args = ap.parse_args()

    meta_path = Path(args.meta_input_json).expanduser().resolve()
    scf_path = Path(args.scf_export).expanduser().resolve()
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    meta_input = load_json(meta_path)
    enums = scf_enums_from_export(scf_path)
    MetaSchema = build_model(enums)

    parsed, raw = call_openai_structured(args.model, MetaSchema, meta_input, args.reasoning_effort)
    if parsed is None:
        raw_path = outdir / (meta_path.stem.replace(".meta_input", "") + ".meta_response_raw.json")
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        raise RuntimeError(f"Parse failure/refusal. Raw: {raw_path}")

    out = postprocess(parsed.model_dump(), meta_input)

    base = meta_path.stem.replace(".meta_input", "")
    out_path = outdir / f"{base}.meta_parsed.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.write_raw:
        raw_path = outdir / f"{base}.meta_response_raw.json"
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Wrote: {out_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

