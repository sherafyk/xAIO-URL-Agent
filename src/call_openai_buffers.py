#!/usr/bin/env python3
"""call_openai_buffers.py

Buffered Panels (Option 2: 12 separate calls)

Input:  *.xaio_parsed.json (produced by merge_xaio.py)
Output: *.buffers.json

Design goals:
- Uses ONLY the provided text (no web browsing)
- Adds sentence IDs (s1, s2, ...) so the model can cite support_spans
- Runs 12 independent model calls (one per panel prompt)
- Stores a canonical JSON object for traceability
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from env_bootstrap import load_repo_env
from logging_utils import elapsed_ms, log_event, setup_logging
from openai_compat import chat_completion_text


load_repo_env()
logger = setup_logging("call_openai_buffers")


def now_utc_ymd_hms() -> str:
    # Matches your SCF date_time_picker display/return format in the export.
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def sha256_hex(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def safe_str(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    return str(x)


def first_str(*vals: Any) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def normalize_ws(s: str) -> str:
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    # Keep newlines for bullet-ish handling, but collapse huge whitespace.
    s = re.sub(r"[\t\f\v]+", " ", s)
    return s


def split_sentences(text: str) -> List[str]:
    """Deterministic sentence-ish splitter.

    We intentionally keep this lightweight (no NLP deps) and stable.
    Output must be stable across reruns for the same input text.
    """
    t = normalize_ws(text)
    if not t.strip():
        return []

    # Break on paragraph boundaries first.
    paras = re.split(r"\n{2,}", t)
    out: List[str] = []

    def split_by_punct(s: str) -> List[str]:
        s = s.strip()
        if not s:
            return []
        # Split on end punctuation followed by whitespace and a likely sentence starter.
        parts = re.split(r"(?<=[.!?])\s+(?=[\"“”'‘\(\[]?[A-Z0-9])", s)
        if len(parts) <= 1:
            return [s]
        return [p.strip() for p in parts if p.strip()]

    for para in paras:
        para = para.strip("\n ")
        if not para:
            continue

        # If this paragraph has multiple lines, treat each line as a unit first
        # (helps with bullet lists, transcripts, etc.).
        if "\n" in para:
            for ln in [x.strip() for x in para.split("\n") if x.strip()]:
                # Strip common bullet prefixes but keep the text.
                ln = re.sub(r"^\s*[\-\*•·]+\s+", "", ln)
                out.extend(split_by_punct(ln))
        else:
            out.extend(split_by_punct(para))

    # Final cleanup: collapse internal whitespace
    cleaned: List[str] = []
    for s in out:
        s2 = re.sub(r"\s+", " ", s).strip()
        if s2:
            cleaned.append(s2)
    return cleaned


def truncate_text_keep_head(text: str, max_chars: int) -> Tuple[str, bool]:
    if max_chars <= 0:
        return text, False
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


BASE_SYSTEM_PROMPT = """You are extracting structured analysis from a provided text.
Do NOT use outside knowledge or web browsing.
Describe things only \"as presented in the text\" (not verified truth).
Be neutral, avoid moralizing.
When uncertain, say so explicitly.
Use only the provided sentence IDs (s1, s2, …) in support_spans.
Output only the requested panel text, ending with the required [PARSE] block.
"""


PANEL_PROMPTS: Dict[str, str] = {
    "aio_panel_01_intent_buffer": """PANEL 1 — Speech-act / Intent Map (Buffer)

Write 1–2 short paragraphs describing what the text is trying to do (speech-act), not whether it’s true.
Identify primary intent and any secondary intents.

End with EXACTLY:
[PARSE]
primary_intent=<one of: reporting|explaining|arguing|warning_forecasting|justifying_excusing|attacking_defending_reputation|mobilizing_call_to_action>
secondary_intents=<comma list or none>
confidence=<low|med|high>
support_spans=<comma sentence IDs or none>
needs_verification=<none or short phrase>
[/PARSE]
""",
    "aio_panel_02_story_spine_buffer": """PANEL 2 — Narrative / Story Spine (Buffer)

Write 1–2 short paragraphs summarizing the narrative spine and causal story AS PRESENTED.
Include: central conflict, turning point/why it matters, root cause (as presented), solution path (as presented).

End with EXACTLY:
[PARSE]
central_conflict=<short>
turning_point=<short>
root_cause_as_presented=<short>
solution_path_as_presented=<short>
support_spans=<comma sentence IDs or none>
[/PARSE]
""",
    "aio_panel_03_definitions_buffer": """PANEL 3 — Definitions & Category Choices (Buffer)

Write 1–2 short paragraphs describing key terms/labels and how the author implicitly defines or uses them.
Include notable category assignments (who counts as what) and any equivalences (X~Y) that steer interpretation.

End with EXACTLY:
[PARSE]
key_terms=<comma list up to 10>
category_assignments_hint=<short>
equivalences_hint=<short>
confidence=<low|med|high>
support_spans=<comma sentence IDs or none>
[/PARSE]
""",
    "aio_panel_04_evidence_posture_buffer": """PANEL 4 — Evidence Posture & Sourcing Style (Buffer)

Write 1–2 short paragraphs describing HOW the text treats evidence (not verifying it).
Cover: attribution degree, hedging vs certainty, implied proof standard, verification moves shown, anonymous sourcing usage.

End with EXACTLY:
[PARSE]
attribution_degree=<low|med|high>
hedging_level=<low|med|high>
certainty_language_level=<low|med|high>
implied_proof_standard=<journalistic_balance|courtroom|activist_moral|mixed|unclear>
verification_moves=<comma list or none>
anonymous_sources_usage=<none|limited|heavy>
evidence_gaps_hint=<short>
support_spans=<comma sentence IDs or none>
[/PARSE]
""",
    "aio_panel_05_uncertainty_buffer": """PANEL 5 — Uncertainty, Unknowns & Disputed Terrain (Buffer)

Write 1–2 short paragraphs summarizing what the text itself marks as unclear/unknown/unconfirmed,
and any explicit disputes (A says X, B says not-X). Do not add outside knowledge.

End with EXACTLY:
[PARSE]
unknowns_count=<0-12>
disputed_points_count=<0-12>
missing_data_flags_hint=<short>
confidence=<low|med|high>
support_spans=<comma sentence IDs or none>
needs_verification=<none or short phrase>
[/PARSE]
""",
    "aio_panel_06_omissions_buffer": """PANEL 6 — Omissions & Selection Effects (Buffer)

Write 1–2 short paragraphs describing measurable coverage gaps WITHOUT mind-reading.
Cover: quoted vs absent stakeholders, evidence types present vs plausibly expected but absent,
time-window selection, alternative hypotheses not addressed, and open questions.

End with EXACTLY:
[PARSE]
quoted_stakeholders_hint=<comma list up to 8 or none>
absent_stakeholders_hint=<comma list up to 8 or none>
evidence_types_present=<comma list: documents|data|video|audio|none>
evidence_types_absent=<comma list or none>
time_window_selection=<short or none>
open_questions_count=<5-15 or 0>
confidence=<low|med|high>
[/PARSE]
""",
    "aio_panel_07_rhetoric_buffer": """PANEL 7 — Rhetoric, Framing & Persuasion Mechanics (Buffer)

Write 1–2 short paragraphs describing rhetoric/framing signals neutrally.
Cover: dominant frames, tone, persuasion devices, anecdote vs aggregate balance, and any loaded language.

End with EXACTLY:
[PARSE]
tone=<neutral|alarmed|empathetic|hostile|advocacy|mixed>
dominant_frames=<comma list: security|humanitarian|legal|economic|cultural|political|other>
persuasion_devices=<comma list or none>
anecdote_vs_aggregate=<anecdote_heavy|balanced|data_heavy|unclear>
loaded_language_hint=<comma list up to 10 or none>
confidence=<low|med|high>
support_spans=<comma sentence IDs or none>
[/PARSE]
""",
    "aio_panel_08_normative_buffer": """PANEL 8 — Normative Layer: Values & Prescriptions (Buffer)

Write 1–2 short paragraphs separating descriptive content from normative content.
Extract value judgments, prescriptions, and implied moral framework (as presented).

End with EXACTLY:
[PARSE]
value_judgments_count=<0+>
prescriptions_count=<0+>
moral_frameworks=<comma list: rights_based|security_based|utilitarian|virtue|other|mixed>
confidence=<low|med|high>
support_spans=<comma sentence IDs or none>
[/PARSE]
""",
    "aio_panel_09_predictions_buffer": """PANEL 9 — Predictions & Commitments (Buffer)

Write 1–2 short paragraphs capturing forecasts and conditional statements to revisit later,
plus any actor commitments reported.

End with EXACTLY:
[PARSE]
predictions_count=<0-12>
commitments_count=<0-12>
dominant_time_horizon=<days|weeks|months|years|unspecified>
confidence=<low|med|high>
support_spans=<comma sentence IDs or none>
[/PARSE]
""",
    "aio_panel_10_actor_map_buffer": """PANEL 10 — Actor Map & Responsibility Attributions (Buffer)

Write 1–2 short paragraphs describing the actor model AS PRESENTED.
Include key actors, roles, relationships, and responsibility attributions (blame/credit/causation/legal) as claimed.

End with EXACTLY:
[PARSE]
key_actors_hint=<comma list up to 10>
roles_hint=<short>
relationships_hint=<short>
responsibility_attributions_count=<0+>
confidence=<low|med|high>
support_spans=<comma sentence IDs or none>
[/PARSE]
""",
    "aio_panel_11_internal_consistency_buffer": """PANEL 11 — Internal Consistency (Buffer)

Write 1–2 short paragraphs checking ONLY for within-text issues (no external checking):
timeline inconsistencies, numerical mismatches, definition drift, contradictions.

End with EXACTLY:
[PARSE]
issues_count=<0-10>
most_severe_type=<timeline|number|definition|contradiction|none>
confidence=<low|med|high>
support_spans=<comma sentence IDs or none>
[/PARSE]
""",
    "aio_panel_12_falsifiability_buffer": """PANEL 12 — Falsifiability: What Would Change My Mind? (Buffer)

Write 1–2 short paragraphs listing 1–5 major theses in the text and what evidence would materially
update or disconfirm each. Mark clearly as analytic scaffolding.

End with EXACTLY:
[PARSE]
theses_count=<1-5 or 0>
top_thesis_hint=<short>
confidence=<low|med|high>
support_spans=<comma sentence IDs or none>
needs_verification=<none or short phrase>
[/PARSE]
""",
}


PANEL_ORDER: List[str] = list(PANEL_PROMPTS.keys())


def build_article_wrapper(xaio: Dict[str, Any], *, max_chars: int) -> Tuple[str, Dict[str, Any]]:
    """Return (wrapper_text, facts) where wrapper_text is the prompt body shared across panels."""

    title = first_str(
        xaio.get("url_content_title"),
        xaio.get("title"),
        xaio.get("content_title"),
    )
    canonical_url = first_str(xaio.get("canonical_url"))
    org = first_str(xaio.get("organization_name"), xaio.get("site_name"))
    authors = xaio.get("author_names") if isinstance(xaio.get("author_names"), list) else []
    author = first_str(authors[0] if authors else "")
    published_at = first_str(xaio.get("published_at"))

    full_text = safe_str(xaio.get("extracted_text_full", ""))
    full_text, truncated = truncate_text_keep_head(full_text, max_chars=max_chars)
    sentences = split_sentences(full_text)

    wrapper_lines: List[str] = [
        "ARTICLE_META",
        f"title: {title}",
        f"canonical_url: {canonical_url}",
        f"publisher/organization: {org}",
        f"author/byline: {author}",
        f"published_at (if known): {published_at}",
        "",
        "ARTICLE_TEXT_WITH_SENTENCE_IDS",
    ]
    for i, s in enumerate(sentences, start=1):
        wrapper_lines.append(f"s{i}: {s}")

    facts = {
        "title": title,
        "canonical_url": canonical_url,
        "organization": org,
        "author": author,
        "published_at": published_at,
        "sentence_count": len(sentences),
        "text_char_count_used": len(full_text),
        "text_truncated": truncated,
        "text_sha256": sha256_hex(full_text),
    }
    return "\n".join(wrapper_lines), facts


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((RateLimitError, APITimeoutError, APIConnectionError, APIError)),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def call_panel(model: str, *, shared_wrapper: str, panel_prompt: str) -> str:
    client = OpenAI()
    user_content = f"{shared_wrapper}\n\n{panel_prompt}\n"
    return chat_completion_text(
        client,
        model=model,
        system_prompt=BASE_SYSTEM_PROMPT,
        user_content=user_content,
    )


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def all_panels_present(obj: Dict[str, Any]) -> bool:
    for k in PANEL_ORDER:
        if not isinstance(obj.get(k), str) or not obj.get(k).strip():
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run xAIO buffered panels (Option 2: 12 separate calls) on a *.xaio_parsed.json.",
    )
    ap.add_argument("xaio_parsed_json", help="Path to *.xaio_parsed.json")
    ap.add_argument("--outdir", default="./out_buffers", help="Where to write *.buffers.json")
    ap.add_argument("--model", default="gpt-4.1-mini", help="OpenAI model for buffered panels")
    ap.add_argument("--schema-version", default="0.1.0", help="Schema/prompt version string")
    ap.add_argument(
        "--max-chars",
        type=int,
        default=120_000,
        help="Max characters of extracted_text_full to include in prompts (head truncation). 0 = no limit.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Recompute panels even if an up-to-date buffers.json already exists.",
    )
    args = ap.parse_args()

    xaio_path = Path(args.xaio_parsed_json).expanduser().resolve()
    item_id = xaio_path.stem.replace(".xaio_parsed", "")
    start = time.monotonic()
    log_event(logger, stage="buffers_start", item_id=item_id, message=f"model={args.model}")

    try:
        xaio = load_json(xaio_path)
        shared_wrapper, facts = build_article_wrapper(xaio, max_chars=int(args.max_chars))

        outdir = Path(args.outdir).expanduser().resolve()
        outdir.mkdir(parents=True, exist_ok=True)
        out_path = outdir / f"{item_id}.buffers.json"

        # If output exists, try to resume (Option 2 makes this worth it).
        existing: Dict[str, Any] = {}
        if out_path.exists():
            try:
                existing = load_json(out_path)
            except Exception:
                existing = {}

        # Skip if already complete for same text sha and not forcing.
        if (
            not args.force
            and existing
            and existing.get("text_sha256") == facts["text_sha256"]
            and all_panels_present(existing)
        ):
            log_event(
                logger,
                stage="buffers_skip",
                item_id=item_id,
                elapsed_ms_value=elapsed_ms(start),
                message=f"already complete sha={facts['text_sha256'][:12]}",
            )
            return 0

        out: Dict[str, Any] = {}
        # Preserve any existing panels to support resuming.
        out.update({k: v for k, v in existing.items() if k in PANEL_ORDER and isinstance(v, str) and v.strip()})

        # Canonical metadata
        out["aio_schema_version"] = str(args.schema_version)
        out["aio_generated_at"] = now_utc_ymd_hms()
        out["aio_model"] = str(args.model)
        out["title"] = facts["title"]
        out["canonical_url"] = facts["canonical_url"]
        out["publisher_organization"] = facts["organization"]
        out["author_byline"] = facts["author"]
        out["published_at"] = facts["published_at"]
        out["sentence_count"] = facts["sentence_count"]
        out["text_char_count_used"] = facts["text_char_count_used"]
        out["text_truncated"] = facts["text_truncated"]
        out["text_sha256"] = facts["text_sha256"]

        # Run any missing panels.
        for k in PANEL_ORDER:
            if isinstance(out.get(k), str) and out.get(k).strip():
                continue

            t0 = time.monotonic()
            log_event(logger, stage="panel_start", item_id=item_id, message=k)
            panel_text = call_panel(args.model, shared_wrapper=shared_wrapper, panel_prompt=PANEL_PROMPTS[k])
            out[k] = (panel_text or "").strip()
            log_event(
                logger,
                stage="panel_done",
                item_id=item_id,
                elapsed_ms_value=elapsed_ms(t0),
                message=k,
            )

            # Persist after each panel so a later retry can resume.
            write_json(out_path, out)

        # Final write (pretty JSON)
        write_json(out_path, out)

        log_event(
            logger,
            stage="buffers_done",
            item_id=item_id,
            elapsed_ms_value=elapsed_ms(start),
            message=f"wrote={out_path}",
        )
        return 0

    except Exception as exc:
        log_event(
            logger,
            stage="buffers_failed",
            item_id=item_id,
            elapsed_ms_value=elapsed_ms(start),
            message=f"{type(exc).__name__}: {exc}",
            level=logging.ERROR,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
