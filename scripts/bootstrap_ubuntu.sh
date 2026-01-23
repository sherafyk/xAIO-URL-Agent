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
  echo "ERROR: python3 not found. Install Python 3.10+." >&2
  exit 1
fi

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="${PY_VER%.*}"
PY_MINOR="${PY_VER#*.}"

if [[ "$PY_MAJOR" -lt "$PY_MIN_MAJOR" ]] || { [[ "$PY_MAJOR" -eq "$PY_MIN_MAJOR" ]] && [[ "$PY_MINOR" -lt "$PY_MIN_MINOR" ]]; }; then
  echo "ERROR: Python $PY_VER found. Need Python ${PY_MIN_MAJOR}.${PY_MIN_MINOR}+." >&2
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
  echo "ERROR: requirements.txt not found." >&2
  exit 1
fi

# 5) Install Playwright browsers (needed even when connecting to an external browser)
if python -c "import playwright" >/dev/null 2>&1; then
  echo "Ensuring Playwright browsers are installed..."
  python -m playwright install chromium >/dev/null 2>&1 || true
fi

# 6) Initialize runtime directory (.runtime)
echo "Initializing .runtime/..."
"$PROJECT_DIR/scripts/init_runtime.sh"

# 7) Print next steps
cat <<NEXT

Next steps (one-time):

1) Edit your runtime config:
     $PROJECT_DIR/.runtime/.env
     $PROJECT_DIR/.runtime/config.yaml

2) Replace your Google service account file:
     $PROJECT_DIR/.runtime/secrets/service_account.json
   Make sure that service account email has access to your Google Sheet.

3) Run environment checks:
     $PROJECT_DIR/scripts/doctor.sh

4) Run the pipeline manually (first run):
     source $PROJECT_DIR/venv/bin/activate
     python $PROJECT_DIR/src/pipeline_run.py --config $PROJECT_DIR/.runtime/config.yaml

5) Optional: install systemd user units (timers/services):
     $PROJECT_DIR/scripts/install_systemd_user.sh
NEXT

echo
echo "Bootstrap complete."
