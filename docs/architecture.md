# Architecture

This repo implements a local-first, browser-backed ingestion pipeline for xAIO.

## Why local-first?
Some sites block server-style scraping. This pipeline runs on a real Ubuntu desktop using a real browser session (Brave via Chrome DevTools Protocol). The goal is reliability and auditability, not stealth.

## Pipeline stages

### Stage 0: Intake queue
- Google Sheet is the queue and state machine.

### Stage 1: Capture (Worker A)
- Reads new URLs from the Sheet.
- Fetches content (HTTP where possible; browser fallback where needed).
- Extracts text + metadata.
- Writes a ground-truth capture JSON to disk.
- Updates Sheet with status + json_path.

Output:
- `out/YYYY/MM/DD/<id>.json` (ground truth)

### Stage 2: Reduce for AI
- Reads capture JSON.
- Produces AI input envelope JSON that:
  - keeps full extracted text
  - keeps small metadata whitelist
  - removes raw HTML and noisy dumps

Output:
- `out_ai/<id>.ai_input.json`

### Stage 3A: AI Metadata parse (Call #1)
- Sends ai_input WITHOUT the bulky body text.
- Extracts/normalizes classification fields (mode/language/status).
- Deterministic fields (domain/site_name/timestamps/counts) are filled from ai_input.

Output:
- `out_meta/<id>.meta_parsed.json`

### Stage 3B: AI Claims extraction (Call #2)
- Sends full extracted text + minimal context.
- Returns only claims: `claim_text`, `claim_type`.
- No verdict, no scoring (verification pipeline comes later).

Output:
- `out_claims/<id>.claims_parsed.json`

### Stage 4: Merge
- Merges meta + claims + verbatim extracted_text_full + counts.
- Produces final WordPress-ready JSON aligned to SCF fields.

Output:
- `out_xaio/<id>.xaio_parsed.json`

## Trust model
- Capture JSON is the source of truth.
- AI outputs are derived artifacts.
- Future stages can re-run AI on the same capture without re-fetching.
