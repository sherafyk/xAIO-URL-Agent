# xAIO URL Agent

A local, browser-backed pipeline for capturing URL content, reducing it for AI parsing, extracting metadata/claims, and publishing WordPress-ready outputs. It is designed for reliability and traceability: every stage writes artifacts to disk, and the system is safe to re-run.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Install](#install)
- [Configuration](#configuration)
- [Logging](#logging)
- [Running the Pipeline](#running-the-pipeline)
- [Systemd Services](#systemd-services)
- [Diagnostics & Troubleshooting](#diagnostics--troubleshooting)
- [Common Fixes](#common-fixes)

## Overview

This project watches a Google Sheet for URLs, fetches pages using HTTP or a real browser (Brave via CDP), stores a canonical capture JSON, then runs staged AI parsing (metadata and claims) to produce WordPress-ready output aligned to SCF fields. The pipeline emphasizes:

- **Ground-truth captures** (stored before AI touches anything).
- **Small, predictable AI calls** instead of a single monolithic call.
- **Idempotency** (safe to re-run without duplicates).

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
```

Each stage is replayable. If something fails, fix the root cause and re-run the specific stage.

## Requirements

- **OS:** Ubuntu LTS (desktop is recommended)
- **Python:** 3.10+ with `venv`
- **Browser:** Brave (beta recommended) with remote debugging enabled
- **APIs:**
  - Google Sheets (service account)
  - OpenAI API key

## Install

```bash
git clone https://github.com/sherafyk/xAIO-URL-Agent.git
cd xAIO-URL-Agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Copy config templates:

```bash
cp config/config.example.yaml config.yaml
cp .env.example .env
```

## Configuration

### Config file (`config.yaml`)

Edit `config.yaml` to set:

- Google Sheet ID and column mappings
- Output directories
- Prompt set IDs
- SQLite path

### Secrets

Place your Sheets credentials at:

```
secrets/service_account.json
```

### Environment variables (`.env`)

Key environment variables:

- `OPENAI_API_KEY`
- `WP_USERNAME`, `WP_APP_PASSWORD`
- `XAIO_CONFIG_PATH` (optional; defaults to `config.yaml`)
- `XAIO_LOG_LEVEL` (default `INFO`)
- `XAIO_LOG_FILE` (default `.runtime/logs/xaio.log`)

Load environment variables:

```bash
source .env
```

## Logging

All services write to **stdout** and **a timestamped log file**. By default, logs are written to:

```
.runtime/logs/xaio.log
```

Override with:

```
XAIO_LOG_FILE="/path/to/xaio.log"
```

## Running the Pipeline

### Manual runs

```bash
source venv/bin/activate
python src/agent.py --config config.yaml
python src/condense_queue.py --config config.yaml
python src/ai_queue.py --config config.yaml
```

You can also run the entire pipeline runner:

```bash
python src/pipeline_run.py --config config.yaml
```

### Update + restart helpers

```bash
chmod +x scripts/update_and_restart.sh
./scripts/update_and_restart.sh
```

## Systemd Services

### Install (recommended for long-running use)

```bash
chmod +x scripts/install_systemd_user.sh
./scripts/install_systemd_user.sh
systemctl --user daemon-reload
systemctl --user enable --now pipeline.timer
```

### Symlink units from repo (advanced)

```bash
chmod +x scripts/deploy_systemd_from_repo.sh
./scripts/deploy_systemd_from_repo.sh
```

This makes the repo the source of truth for systemd unit files.

### Monitor

```bash
systemctl --user status pipeline.service
journalctl --user -u pipeline.service -f
```

## Diagnostics & Troubleshooting

### 1) Verify unit files are valid

```bash
systemd-analyze verify systemd/user/pipeline.service
```

### 2) Check for masked or missing units

```bash
systemctl --user list-unit-files | rg xaio
systemctl --user status pipeline.service
```

### 3) Inspect logs

```bash
journalctl --user -u pipeline.service -n 200 --no-pager
cat .runtime/logs/xaio.log | tail -n 200
```

### 4) Confirm your repo path

Systemd unit templates default to `%h/xAIO-URL-Agent`. If your repo lives elsewhere, re-install from the correct directory:

```bash
cd /path/to/xAIO-URL-Agent
./scripts/install_systemd_user.sh
systemctl --user daemon-reload
```

## Common Fixes

### "Unit has a bad unit file setting"

Cause: invalid settings (often environment variables in `EnvironmentFile=` paths).

Fix:

1. Update to the latest repo.
2. Reinstall systemd units:

```bash
./scripts/install_systemd_user.sh
systemctl --user daemon-reload
```

### "Unit is masked"

Unmask and re-enable:

```bash
systemctl --user unmask pipeline.service pipeline.timer
systemctl --user enable --now pipeline.timer
```

### "Unit not found"

The unit files were not installed or not loaded:

```bash
./scripts/install_systemd_user.sh
systemctl --user daemon-reload
systemctl --user start pipeline.service
```

---

If you encounter issues beyond these steps, run `scripts/doctor.sh` and include the output along with the relevant sections from `.runtime/logs/xaio.log` and `journalctl`.
