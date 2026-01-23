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

> If you are brand-new to coding, do **exactly** the commands below in order. Copy/paste is fine.

### 4.1 Get the code onto your machine

You have two ways to get the code:

**Option A (recommended): Git clone (best for updates)**

```bash
git clone <your-repo-url>
cd xAIO-URL-Agent
```

**Option B: Download ZIP (works, but you must re-download for updates)**

1. Download the ZIP from GitHub
2. Unzip it
3. Open a Terminal in the unzipped folder

---

### 4.2 Fix permissions (so you don’t get “Permission denied”)

If you ever see:

* `Permission denied`
* or a script won’t run

It usually means your computer is not allowing the file to execute.

Run this ONCE after you download/unzip OR after you pull new code:

```bash
# From the repo root folder:
chmod +x scripts/*.sh || true
chmod +x src/*.py || true
```

If that still doesn’t fix it, see **Section 4.6 (Permission denied — advanced causes)**.

---

### 4.3 Make scripts executable (required)

If you already did the permission fix above, you can skip this. Otherwise:

```bash
chmod +x scripts/*.sh
```

---

### 4.4 Bootstrap the machine (system dependencies + venv)

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

### 4.5 If you update the code later (the “update” recipe)

If you used **Option A (git clone)**:

```bash
# From the repo root
cd xAIO-URL-Agent

git pull

# Fix permissions again (safe even if not needed)
chmod +x scripts/*.sh || true

# If Python dependencies changed:
source venv/bin/activate
pip install -r requirements.txt
```

If you used **Option B (ZIP)**:

* Download the new ZIP
* Unzip it
* Run the permission fix:

```bash
chmod +x scripts/*.sh || true
```

---

### 4.6 Permission denied — advanced causes (stupid-proof checklist)

If you still get `Permission denied` AFTER running `chmod +x` above, it is usually one of these:

#### Cause A: Your folder is on a “noexec” drive (scripts cannot run there)

This happens on:

* some external drives
* some corporate shared folders
* some special mounts

Check:

```bash
mount | grep -E "\$(pwd)|noexec" || true
```

If you see `noexec`, move the project somewhere normal, like your home folder:

```bash
mkdir -p ~/projects
mv /path/to/xAIO-URL-Agent ~/projects/
cd ~/projects/xAIO-URL-Agent
chmod +x scripts/*.sh || true
```

#### Cause B: Windows line endings (CRLF) in scripts

If scripts complain or behave strangely, fix line endings:

```bash
sudo apt-get update
sudo apt-get install -y dos2unix

dos2unix scripts/*.sh
chmod +x scripts/*.sh || true
```

#### Cause C: You’re trying to run a script without `./`

Example wrong:

```bash
scripts/bootstrap_ubuntu.sh
```

Correct:

```bash
./scripts/bootstrap_ubuntu.sh
```

#### Cause D: You are not in the repo root folder

You must run commands from the folder that contains `scripts/`.

Check:

```bash
ls
```

You should see `scripts` and `src`.

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

## 12. Source of truth checklist (stupid-proof)

If something breaks, check in this exact order:

1. Are you in the correct folder?

```bash
ls
```

You should see: `scripts` and `src`.

2. Fix permissions (safe to run any time)

```bash
chmod +x scripts/*.sh || true
chmod +x src/*.py || true
```

3. Confirm your local config exists

```bash
ls -la .runtime
ls -la .runtime/.env
ls -la .runtime/config.yaml
```

4. Run the health check

```bash
./scripts/doctor.sh
```

5. Run manually (always before systemd)

```bash
source venv/bin/activate
python src/pipeline_run.py --config .runtime/config.yaml
```

6. If systemd fails, read the logs

```bash
journalctl --user -u pipeline.service -n 200 --no-pager
```

---

## 13. Final note

If you are debugging while exhausted or angry: **stop, run doctor.sh, read the logs from the top**, not the bottom.

This system is deterministic. Chaos only comes from configuration drift — which this repo now explicitly prevents.

You are not crazy. This *is* solvable.
