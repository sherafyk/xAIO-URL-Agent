## Notes

* Use a dedicated user-data-dir for the agent so it doesn't interfere with your daily browsing profile.
* If multiple Brave instances are running, the debugging port can become unreliable. Prefer a dedicated instance for the agent.

````

## `docs/systemd.md`

```md
# systemd (user services)

We run the workers as user-level systemd timers so they survive reboots and run without a terminal.

## Where units live
User units:
~/.config/systemd/user/

Repo copies (source-controlled):
systemd/user/

## Common commands

Reload definitions:
```bash
systemctl --user daemon-reload
````

Enable a timer:

```bash
systemctl --user enable --now url-agent.timer
```

Disable a timer:

```bash
systemctl --user disable --now url-agent.timer
```

See timers:

```bash
systemctl --user list-timers
```

Logs:

```bash
journalctl --user -u url-agent.service -n 200 --no-pager
journalctl --user -u condense-agent.service -n 200 --no-pager
```

Follow logs live:

```bash
journalctl --user -u url-agent.service -f
```

## Overlap prevention

Use `flock` in ExecStart so multiple invocations don't overlap.

````

## `docs/troubleshooting.md`

```md
# Troubleshooting

## It keeps opening Brave windows / too many tabs
- Ensure you are attaching to an existing Brave instance (CDP), not launching a new instance per URL.
- Confirm only one Brave debug instance is running.

## CDP not reachable
```bash
curl -s http://127.0.0.1:9222/json/version
````

If that fails:

* Brave isn't running with `--remote-debugging-port=9222`
* another process is using the port

## Sheet updates not working

Check:

* `secrets/service_account.json` exists
* the service account email has access to the sheet
* spreadsheet URL and worksheet name are correct in config.yaml

## systemd timer runs but nothing happens

* check logs with journalctl
* run the script manually to see exceptions
* verify working directory paths in the unit file

## AI errors

* confirm OPENAI_API_KEY is set
* confirm scf-export-content.json exists and is referenced correctly
* inspect `*.response_raw.json` outputs for schema issues

````

---

# 5) Add the turnkey bootstrap script you requested

## `scripts/bootstrap_ubuntu.sh` ✅

This script does exactly what you asked:

- checks Python version
- creates venv
- installs requirements
- creates required directories
- copies config template → local config if missing
- prints next steps (service account, OpenAI key, doctor)

**File:** `scripts/bootstrap_ubuntu.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_MIN_MAJOR=3
PY_MIN_MINOR=10

echo "== xAIO URL Agent bootstrap (Ubuntu) =="
echo "Project: $PROJECT_DIR"
echo

# 1) Check Python version
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.10+."
  exit 1
fi

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="${PY_VER%.*}"
PY_MINOR="${PY_VER#*.}"

if [[ "$PY_MAJOR" -lt "$PY_MIN_MAJOR" ]] || { [[ "$PY_MAJOR" -eq "$PY_MIN_MAJOR" ]] && [[ "$PY_MINOR" -lt "$PY_MIN_MINOR" ]]; }; then
  echo "ERROR: Python $PY_VER found. Need Python ${PY_MIN_MAJOR}.${PY_MIN_MINOR}+."
  exit 1
fi
echo "OK: Python $PY_VER"

# 2) Create venv if missing
if [[ ! -d "$PROJECT_DIR/venv" ]]; then
  echo "Creating venv..."
  python3 -m venv "$PROJECT_DIR/venv"
else
  echo "OK: venv exists"
fi

# 3) Activate venv
# shellcheck disable=SC1091
source "$PROJECT_DIR/venv/bin/activate"
echo "OK: venv activated ($(which python))"

# 4) Install requirements
if [[ -f "$PROJECT_DIR/requirements.txt" ]]; then
  echo "Installing requirements..."
  pip install -U pip
  pip install -r "$PROJECT_DIR/requirements.txt"
  echo "OK: requirements installed"
else
  echo "WARN: requirements.txt not found. Create one (pip freeze > requirements.txt) and re-run."
fi

# 5) Create required directories (safe if already exist)
echo "Creating runtime directories..."
mkdir -p "$PROJECT_DIR/out" \
         "$PROJECT_DIR/out_ai" \
         "$PROJECT_DIR/out_ai_meta" \
         "$PROJECT_DIR/out_meta" \
         "$PROJECT_DIR/out_claims" \
         "$PROJECT_DIR/out_xaio" \
         "$PROJECT_DIR/locks" \
         "$PROJECT_DIR/secrets" \
         "$PROJECT_DIR/examples"
echo "OK: directories ready"

# 6) Ensure local config exists
if [[ -f "$PROJECT_DIR/config.yaml" ]]; then
  echo "OK: config.yaml exists"
else
  if [[ -f "$PROJECT_DIR/config/config.example.yaml" ]]; then
    echo "Creating config.yaml from config/config.example.yaml..."
    cp "$PROJECT_DIR/config/config.example.yaml" "$PROJECT_DIR/config.yaml"
    echo "OK: config.yaml created (edit it before running)"
  elif [[ -f "$PROJECT_DIR/config.example.yaml" ]]; then
    echo "Creating config.yaml from config.example.yaml..."
    cp "$PROJECT_DIR/config.example.yaml" "$PROJECT_DIR/config.yaml"
    echo "OK: config.yaml created (edit it before running)"
  else
    echo "WARN: No config.example.yaml found. Create config/config.example.yaml then re-run."
  fi
fi

# 7) Print next steps
cat <<'NEXT'

Next steps (manual, one-time):
1) Google Sheets service account key:
   - Put your key at:  ./secrets/service_account.json
   - Ensure the service account email has access to the Google Sheet.

2) OpenAI API key:
   - Create a .env file (do NOT commit it) based on .env.example
   - Example:
       cp .env.example .env
       nano .env
       source .env

3) Run the environment checks:
       ./scripts/doctor.sh

4) Optional: install systemd user units (timers/services):
       ./scripts/install_systemd_user.sh
NEXT

echo
echo "Bootstrap complete."
````

Make scripts executable:

```bash
chmod +x scripts/*.sh
```

---

# 6) Add the remaining scripts

## `scripts/doctor.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "== xAIO URL Agent doctor =="
echo "Project: $PROJECT_DIR"
echo

fail=0

say_ok(){ echo "OK   $*"; }
say_warn(){ echo "WARN $*"; }
say_err(){ echo "ERR  $*"; fail=1; }

# Python / venv
if [[ -d venv ]]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
  say_ok "venv present"
else
  say_err "venv missing (run ./scripts/bootstrap_ubuntu.sh)"
fi

python -c "import sys; assert sys.version_info >= (3,10)" >/dev/null 2>&1 \
  && say_ok "Python >= 3.10" || say_err "Python < 3.10"

# Deps
python -c "import yaml" >/dev/null 2>&1 && say_ok "PyYAML installed" || say_warn "PyYAML missing"
python -c "import gspread" >/dev/null 2>&1 && say_ok "gspread installed" || say_warn "gspread missing"
python -c "import openai" >/dev/null 2>&1 && say_ok "openai installed" || say_warn "openai missing"
python -c "import pydantic" >/dev/null 2>&1 && say_ok "pydantic installed" || say_warn "pydantic missing"

# Files
[[ -f config.yaml ]] && say_ok "config.yaml present" || say_err "config.yaml missing (copy from config.example.yaml)"
[[ -f secrets/service_account.json ]] && say_ok "secrets/service_account.json present" || say_warn "service_account.json missing (needed for Sheets)"
[[ -f config/scf-export-content.json || -f scf-export-content.json ]] && say_ok "SCF export present" || say_warn "SCF export missing (needed for enums)"

# Output dirs
for d in out out_ai out_ai_meta out_meta out_claims out_xaio locks; do
  [[ -d "$d" ]] && say_ok "dir exists: $d" || say_warn "dir missing: $d"
done

# OpenAI key
if [[ -n "${OPENAI_API_KEY:-}" ]]; then
  say_ok "OPENAI_API_KEY set"
else
  say_warn "OPENAI_API_KEY not set (source .env)"
fi

# Brave CDP
if command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 1 "http://127.0.0.1:9222/json/version" >/dev/null 2>&1; then
    say_ok "Brave CDP reachable on 127.0.0.1:9222"
  else
    say_warn "Brave CDP not reachable (launch Brave with --remote-debugging-port=9222)"
  fi
else
  say_warn "curl not installed (sudo apt install -y curl)"
fi

echo
if [[ "$fail" -eq 0 ]]; then
  echo "Doctor: ✅ no hard failures."
else
  echo "Doctor: ❌ failures found. Fix ERR items above."
  exit 1
fi
```

## `scripts/brave_debug_launch.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

PORT="${BRAVE_CDP_PORT:-9222}"
PROFILE_DIR="${BRAVE_PROFILE_DIR:-$HOME/.config/BraveSoftware/Brave-Browser-Beta-Profile-xaio}"

echo "Launching Brave Beta with CDP on port $PORT"
echo "Profile: $PROFILE_DIR"

mkdir -p "$PROFILE_DIR"

# Try common binary names
if command -v brave-browser-beta >/dev/null 2>&1; then
  BIN="brave-browser-beta"
elif command -v brave-browser >/dev/null 2>&1; then
  BIN="brave-browser"
else
  echo "ERROR: Brave not found. Install Brave or update the script to your binary."
  exit 1
fi

exec "$BIN" --remote-debugging-port="$PORT" --user-data-dir="$PROFILE_DIR"
```

## `scripts/install_systemd_user.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$PROJECT_DIR/systemd/user"
DEST_DIR="$HOME/.config/systemd/user"

mkdir -p "$DEST_DIR"

echo "Installing user systemd units..."
cp -v "$SRC_DIR"/*.service "$DEST_DIR"/
cp -v "$SRC_DIR"/*.timer "$DEST_DIR"/

systemctl --user daemon-reload

echo "Enable timers:"
echo "  systemctl --user enable --now url-agent.timer"
echo "  systemctl --user enable --now condense-agent.timer"
```

## `scripts/update_and_restart.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "Pulling latest changes..."
git pull --rebase

echo "Restarting timers (if installed)..."
systemctl --user daemon-reload || true
systemctl --user restart url-agent.timer 2>/dev/null || true
systemctl --user restart condense-agent.timer 2>/dev/null || true

echo "Done."
```

---

# 7) (Optional) Add `config/config.example.yaml`

If you already have one, keep it. If not, here’s a safe template you can commit:

**File:** `config/config.example.yaml`

```yaml
sheet:
  spreadsheet_url: "https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit"
  worksheet_name: "Sheet1"
  header_row: 1
  first_data_row: 2

columns:
  url: "A"
  status: "B"
  json_path: "F"

columns_ai:
  ai_status: "I"
  ai_input_path: "J"
  ai_error: "K"

agent_ai:
  out_ai_dir: "./out_ai"
  prompt_set_id: "xaio-v1"
  max_per_run: 50
```

---

# 8) Commit + push

```bash
git add .
git commit -m "Add docs + scripts + bootstrap + repo templates"
git push
```

---

## After this, new machine setup becomes:

```bash
git clone <repo>
cd xAIO-url-agent
chmod +x scripts/*.sh
./scripts/bootstrap_ubuntu.sh

# put secrets/service_account.json
cp /path/to/service_account.json ./secrets/service_account.json

# set OpenAI key
cp .env.example .env
nano .env
source .env

./scripts/doctor.sh
./scripts/install_systemd_user.sh
```

---

If you want, paste your current repo root `ls -la` (just filenames) and I’ll tell you what to move into `src/` vs leave at root so the repo reads cleanly to both humans and an agent.
