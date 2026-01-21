# xAIO URL Agent – End‑to‑End Pipeline Documentation

> **Purpose:** This repository implements a **local, human‑browser‑backed content ingestion and analysis pipeline** for xAIO. It watches a Google Sheet for URLs, fetches pages using a real desktop browser when needed, captures clean text + metadata, and then runs **multi‑stage AI parsing** (metadata first, claims later) to populate a WordPress Custom Post Type (SCF‑based) in a controlled, auditable way.

This README is written as if you are a **brand‑new developer** onboarding to the project.

---

## 1. High‑Level Concept

This project intentionally avoids “headless scraping + monolithic AI calls.” Instead, it:

1. Uses a **local Ubuntu machine** as a physical agent (real browser, real IP, real session).
2. Separates concerns into **clear pipeline stages**.
3. Preserves **ground‑truth captures** before any AI touches the data.
4. Uses **multiple small AI calls** instead of one expensive, overloaded call.
5. Treats AI as a *parser*, not a crawler and not a fact‑checker (yet).

At a glance:

```
Google Sheet (URL queue)
        ↓
[agent.py]  ── fetch + capture ──▶ out/YYYY/MM/DD/*.json
        ↓
[reduce4ai.py] ──▶ out_ai/*.ai_input.json
        ↓
──────────────── AI STAGE ────────────────
        ↓
[strip_content_for_meta.py]
        ↓
[call_openai_meta.py]   ──▶ out_meta/*.meta_parsed.json
        ↓
[call_openai_claims.py] ──▶ out_claims/*.claims_parsed.json
        ↓
[merge_xaio.py]         ──▶ out_xaio/*.xaio_parsed.json
```

---

## 2. What This Project Is (and Is Not)

### ✅ This project **IS**:

* A **local‑first ingestion pipeline**
* Browser‑backed (Brave via Chrome DevTools Protocol)
* Google Sheets–driven (simple intake UX)
* Deterministic, auditable, replayable
* Designed for **xAIO / AIO / trust & integrity workflows**

### ❌ This project **IS NOT**:

* A SaaS crawler
* A headless scraping farm
* A fact‑checking engine (that comes later)
* A one‑shot “summarize this URL” script

---

## 3. Environment Requirements

### Operating System

* **Ubuntu LTS** (tested on desktop, not server)

### Python

* Python **3.10+**
* Virtual environment required

### Browser

* **Brave Browser (Beta recommended)**
* Must be launched with remote debugging enabled

### External Services

* Google Sheets (service account)
* OpenAI API (Responses API with Structured Outputs)

---

## 4. Local Directory Structure (Authoritative)

```
~/url-agent/
│
├── agent.py                    # Worker A: fetch + capture
├── reduce4ai.py                # Reduce capture → ai_input
│
├── strip_content_for_meta.py   # Remove bulky body text
├── call_openai_meta.py         # AI Call #1 (metadata only)
├── call_openai_claims.py       # AI Call #2 (claims only)
├── merge_xaio.py               # Merge AI outputs
│
├── condense_queue.py           # Sheet-driven reducer worker
│
├── config.yaml                 # Sheet + column mapping
├── scf-export-content.json     # WordPress SCF schema export
│
├── agent.db                    # SQLite idempotency DB
│
├── out/                         # Raw capture JSONs (ground truth)
│   └── YYYY/MM/DD/*.json
│
├── out_ai/                      # Reduced AI input envelopes
├── out_ai_meta/                # ai_input without body text
├── out_meta/                   # Parsed metadata results
├── out_claims/                 # Parsed claims results
├── out_xaio/                   # Final merged output
│
├── locks/                       # flock lock files
├── secrets/
│   └── service_account.json    # Google Sheets credentials
│
├── venv/                        # Python virtualenv
└── README.md                   # This document
```

---

## 5. Google Sheet as the Intake Queue

### Required Columns (minimum)

| Column | Purpose                                       |
| ------ | --------------------------------------------- |
| A      | URL                                           |
| B      | status (NEW / FETCHING / DONE / ERROR)        |
| F      | json_path (written by agent.py)               |
| I      | ai_status (CONDENSING / AI_READY / AI_FAILED) |
| J      | ai_input_path                                 |
| K      | ai_error                                      |

The Sheet acts as a **state machine**, not a database.

---

## 6. Worker A: Fetch + Capture (`agent.py`)

### Responsibilities

* Poll Google Sheet
* Identify new URLs
* Fetch via HTTP **or** Brave browser fallback
* Extract:

  * final URL
  * clean text
  * metadata
* Write **ground‑truth JSON** to `out/`
* Update Sheet status

### Key Design Choice

> **Never discard data at this stage.**

The capture JSON is sacred. Everything else is derived from it.

---

## 7. Reducer: Capture → AI Input (`reduce4ai.py`)

### Purpose

Create a **clean, minimal, AI‑ready envelope** without losing information.

### Input

* `out/YYYY/MM/DD/*.json`

### Output

* `out_ai/*.ai_input.json`

### What It Keeps

* Canonical URL
* Domain
* Site name
* Published / modified hints
* Full extracted text
* Character & word counts

### What It Removes

* Raw HTML
* Massive meta dumps
* Browser artifacts

This file is the **single source of truth for AI calls**.

---

## 8. Why the AI Stage Is Split in Two

### Motivation

* Cost control
* Token limits
* Better determinism
* Clear responsibility boundaries

### AI Call #1: Metadata Only

* No body text
* Classifies content
* Sets taxonomy fields
* Populates SCF identity fields

### AI Call #2: Claims Only

* Full text
* Extracts atomic factual statements
* **No verdicts, no scoring**

This makes later fact‑checking its own independent pipeline.

---

## 9. AI Call #1: Metadata (`call_openai_meta.py`)

### Input

* `out_ai_meta/*.meta_input.json`

### Output

* `out_meta/*.meta_parsed.json`

### AI Is Allowed To Decide

* content_mode
* language
* workflow_status
* intake_kind

### AI Is NOT Allowed To Guess

These are hard‑filled in post‑processing:

* domain
* site_name
* collected_at_utc
* published_at
* modified_time
* char_count / word_count

---

## 10. AI Call #2: Claims (`call_openai_claims.py`)

### Input

* `out_ai/*.ai_input.json`
* `out_meta/*.meta_parsed.json`

### Output

* `out_claims/*.claims_parsed.json`

### Claims Structure

Each claim is:

```json
{
  "claim_text": "Atomic, checkable statement",
  "claim_type": "event | quantity | date_time | quote | …"
}
```

No verdicts. No confidence scores. No sources. Those come later.

---

## 11. Merge Stage (`merge_xaio.py`)

### Purpose

Create a **final WP‑ready JSON** aligned exactly to SCF fields.

### Output

* `out_xaio/*.xaio_parsed.json`

This file can be:

* Written directly to WordPress
* Stored for later verification
* Reprocessed with new scoring models

---

## 12. Systemd Services (Always‑On Behavior)

### Timers

* `url-agent.timer` → fetch & capture
* `condense-agent.timer` → reduce & AI prep

### Safety

* `flock` prevents overlapping runs
* Each worker is idempotent

---

## 13. Development & Debugging

### Manual Runs

```bash
python agent.py
python reduce4ai.py out/…json
python call_openai_meta.py …
python call_openai_claims.py …
```

### Logs

```bash
journalctl --user -u url-agent.service -f
journalctl --user -u condense-agent.service -f
```

### Dashboard

```bash
url-agent-status
```

---

## 14. Design Philosophy (Important)

* **Capture first, interpret later**
* **Never overwrite source truth**
* **AI is a parser, not an authority**
* **Every stage should be replayable**
* **Claims extraction ≠ fact‑checking**

This pipeline is intentionally conservative. That’s a feature.

---

## 15. Where This Goes Next

Planned future workers:

* Claim verification
* Source retrieval
* Cross‑document contradiction detection
* Trust & integrity scoring
* Public xAIO compliance signals

---

## 16. TL;DR for New Devs

1. URLs go into a Google Sheet
2. The local machine fetches them
3. Raw JSON is saved forever
4. AI input is reduced & structured
5. Metadata and claims are extracted separately
6. Outputs map 1‑to‑1 to WordPress fields

If you understand that, you understand the project.
****
