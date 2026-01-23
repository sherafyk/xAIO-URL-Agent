# xAIO URL Agent

A local, browser-backed pipeline for capturing URL content, reducing it for AI parsing, extracting metadata/claims, and publishing WordPress-ready outputs.

This repo is designed for reliability and traceability:
- Each stage writes artifacts to disk
- Each stage is safe to re-run (idempotent-ish)
- The pipeline is split into small AI calls (meta + claims)

## Overview

This project watches a Google Sheet for URLs, fetches pages using HTTP or a real browser (Brave via CDP), stores a canonical capture JSON, then runs staged AI parsing (metadata and claims) to produce WordPress-ready output aligned to SCF fields. The final stage publishes to WordPress via the xAIO ingest endpoint.

## Architecture

Pipeline stages and artifacts:

```
Google Sheet (URL queue)
        ↓
agent.py        → out/YYYY/MM/DD/*.json
        ↓
reduce4ai.py    → out_ai/*.ai_input.json
        ↓
strip_content_for_meta.py → out_ai_meta/*.meta_input.json
        ↓
call_openai_meta.py       → out_meta/*.meta_parsed.json
        ↓
call_openai_claims.py     → out_claims/*.claims_parsed.json
        ↓
merge_xaio.py             → out_xaio/*.xaio_parsed.json
        ↓
wp_upload_queue.py        → WordPress (xaio/v1/ingest)
```

## The only place you should edit local settings

To keep things clean, **all local config & secrets live in one gitignored folder**:

```
.runtime/
  .env                          # ALL env vars + secrets (OpenAI, WP, etc.)
  config.yaml                   # runtime config
  secrets/service_account.json  # Google Sheets service account key
```

Nothing else in the repo should need per-machine edits.

## Quick start (Ubuntu)

```bash
chmod +x scripts/*.sh
./scripts/bootstrap_ubuntu.sh
```

Then:

1) Edit:
- `.runtime/.env`
- `.runtime/config.yaml`

2) Replace:
- `.runtime/secrets/service_account.json`

3) Run:

```bash
./scripts/doctor.sh
source venv/bin/activate
python src/pipeline_run.py --config .runtime/config.yaml
```

## Manual install

```bash
git clone https://github.com/sherafyk/xAIO-URL-Agent.git
cd xAIO-URL-Agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Create .runtime/ + templates
./scripts/init_runtime.sh

# Edit runtime config
tool="${EDITOR:-nano}"
$tool .runtime/.env
$tool .runtime/config.yaml
```

## Configuration

### Config file (`.runtime/config.yaml`)

Edit `.runtime/config.yaml` to set:
- Google Sheet URL + worksheet name
- Column mappings
- Output directories
- Fetch settings (CDP endpoint, etc.)
- WordPress ingest URL
- AI models + reasoning effort

### Environment variables (`.runtime/.env`)

Common variables:
- `OPENAI_API_KEY`
- `WP_USERNAME`, `WP_APP_PASSWORD` (for WordPress ingest)
- `XAIO_LOG_LEVEL`, `XAIO_LOG_FILE`

You normally **do not** need to `source` this file manually because every Python entrypoint calls `load_repo_env()` and loads it automatically.

## Systemd services

Install user services/timers:

```bash
./scripts/install_systemd_user.sh
systemctl --user daemon-reload
systemctl --user enable --now pipeline.timer
```

Monitor:

```bash
systemctl --user status pipeline.service
journalctl --user -u pipeline.service -f
```

## Troubleshooting

Run:

```bash
./scripts/doctor.sh
```

Common issues:
- **Sheets access**: ensure the service account email has edit access to the sheet.
- **CDP not reachable**: ensure Brave is running with remote debugging enabled (port in `.runtime/config.yaml`).
- **OpenAI errors**: verify `OPENAI_API_KEY` in `.runtime/.env` and that the model name exists for your account.
