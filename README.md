# xAIO URL Agent — **Enterprise-Grade, Single Source of Truth**

> This README is intentionally exhaustive. If something can go wrong, it is documented here. If you follow this top‑to‑bottom, the system will run.

---

## 0. Philosophy (Why this exists)

This repository is designed around **one immutable principle**:

> **There must be exactly ONE place to configure secrets and runtime behavior.**

No scattered `.env` files.
No hidden defaults.
No "magic" fallbacks that silently change behavior.

Everything you edit locally lives in:

```
.runtime/
```

If something breaks, you debug **from there outward**.

---

## 1. What this system does (high level)

The pipeline:

1. Captures URLs / pages
2. Normalizes + canonicalizes content
3. Extracts metadata (AI – structured, schema‑validated)
4. Extracts claims (AI – structured, schema‑validated)
5. Writes results to Google Sheets and/or WordPress
6. Runs either manually OR via systemd timers

Every stage is deterministic and restartable.

---

## 2. Directory layout (authoritative)

```
xAIO-URL-Agent/
├── src/                     # All Python runtime code
├── scripts/                 # All operational scripts (bootstrap, doctor, init)
├── systemd/                 # systemd unit templates
├── .runtime/                # 🔥 ONLY place you edit locally 🔥
│   ├── .env                 # ALL secrets + env vars
│   ├── config.yaml          # Runtime behavior config
│   ├── secrets/
│   │   └── service_account.json
│   └── logs/
├── venv/                    # Python virtual environment
└── README.md                # This file
```

Anything outside `.runtime/` should be considered **code**, not configuration.

---

## 3. Absolute prerequisites

### OS

* Ubuntu 20.04+ (22.04 recommended)

### Required packages

Installed automatically by bootstrap, but listed here for clarity:

* python3.10+
* python3-venv
* python3-pip
* build-essential
* curl
* git

---

## 4. First-time setup (clean machine)

### 4.1 Clone the repo

```bash
git clone <your-repo-url>
cd xAIO-URL-Agent
```

---

### 4.2 Make scripts executable (required)

```bash
chmod +x scripts/*.sh
```

---

### 4.3 Bootstrap the machine (system dependencies + venv)

```bash
./scripts/bootstrap_ubuntu.sh
```

What this does:

* Installs system packages
* Creates `venv/`
* Installs Python dependencies
* Verifies Python version

If this fails → **stop and fix it before continuing**.

---

## 5. Initialize runtime (ONE TIME PER MACHINE)

```bash
./scripts/init_runtime.sh
```

This creates:

```
.runtime/
├── .env.example   → copied to .env
├── config.yaml
├── secrets/
│   └── service_account.json (placeholder)
└── logs/
```

---

## 6. Configure the system (ONLY place you edit)

### 6.1 `.runtime/.env` (ALL secrets)

Open it:

```bash
nano .runtime/.env
```

**Required variables**:

```
OPENAI_API_KEY=sk-...
```

**If using WordPress**:

```
WP_BASE_URL=https://example.com
WP_USERNAME=...
WP_APP_PASSWORD=...
```

**If using Google Sheets**:

```
GOOGLE_SERVICE_ACCOUNT_JSON=.runtime/secrets/service_account.json
```

Nothing else in the repo should contain secrets.

---

### 6.2 Google service account

Replace the placeholder:

```bash
cp /path/to/real/service_account.json .runtime/secrets/service_account.json
```

Make sure the Google Sheet is shared with the service account email.

---

### 6.3 `.runtime/config.yaml` (behavior)

This controls:

* Which stages run
* Batch sizes
* Output targets
* Retry behavior

If something behaves "weird", this file is where you look.

---

## 7. Sanity check (DO NOT SKIP)

```bash
./scripts/doctor.sh
```

This verifies:

* Python imports
* `.runtime/.env` loading
* OpenAI connectivity
* Google credentials readability
* Config validity

If `doctor.sh` fails → **the pipeline will fail**.

---

## 8. Running the pipeline manually (DEBUG MODE)

Always test manually before systemd.

```bash
source venv/bin/activate
python src/pipeline_run.py --config .runtime/config.yaml
```

Logs will appear in:

```
.runtime/logs/
```

---

## 9. Understanding failures (READ THIS WHEN MAD)

### 9.1 OpenAI 400 schema errors

Symptoms:

* `Invalid schema`
* `additionalProperties`
* `required must include`

Cause:

* OpenAI strict JSON schema enforcement

Status:

* **Already fixed** in this repo via schema normalization

If you see it again:

* You are running old code
* Or using a stale venv

Fix:

```bash
rm -rf venv
./scripts/bootstrap_ubuntu.sh
```

---

### 9.2 Pipeline service fails immediately

Run:

```bash
journalctl --user -u pipeline.service -n 200 --no-pager
```

**90% of causes**:

* `.runtime/.env` missing
* Wrong path to `config.yaml`
* venv not found

---

## 10. systemd (PRODUCTION MODE)

### 10.1 Install user services

```bash
./scripts/install_systemd_user.sh
systemctl --user daemon-reload
```

---

### 10.2 Enable + start

```bash
systemctl --user enable --now pipeline.timer
```

---

### 10.3 Monitor

```bash
systemctl --user status pipeline.service
journalctl --user -u pipeline.service -f
```

---

## 11. How to recover when things go sideways

### Hard reset (safe)

```bash
systemctl --user stop pipeline.timer
systemctl --user stop pipeline.service
rm -rf venv
./scripts/bootstrap_ubuntu.sh
./scripts/doctor.sh
```

---

## 12. Source of truth checklist (tattoo this)

If something breaks, check in this order:

1. `.runtime/.env`
2. `.runtime/config.yaml`
3. `.runtime/logs/`
4. `doctor.sh`
5. Manual run (never systemd first)

If you follow this order, you will not spiral.

---

## 13. Final note

If you are debugging while exhausted or angry: **stop, run doctor.sh, read the logs from the top**, not the bottom.

This system is deterministic. Chaos only comes from configuration drift — which this repo now explicitly prevents.

You are not crazy. This *is* solvable.
