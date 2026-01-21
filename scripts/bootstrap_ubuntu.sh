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

# Note: Make scripts executable: chmod +x scripts/*.sh
