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

# Load runtime env (non-fatal)
if [[ -f ".runtime/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".runtime/.env"
  set +a
  say_ok "loaded .runtime/.env"
else
  say_warn "missing .runtime/.env (run scripts/init_runtime.sh)"
fi

CONFIG_PATH="${XAIO_CONFIG_PATH:-$PROJECT_DIR/.runtime/config.yaml}"
SA_PATH="${GOOGLE_SERVICE_ACCOUNT_JSON:-$PROJECT_DIR/.runtime/secrets/service_account.json}"

# Python / venv
if [[ -d venv ]]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
  if [[ "${VIRTUAL_ENV:-}" == "$PROJECT_DIR/venv" ]]; then
    say_ok "venv active"
  else
    say_warn "venv present but not active (expected $PROJECT_DIR/venv)"
  fi
else
  say_err "venv missing (run ./scripts/bootstrap_ubuntu.sh)"
fi

python -c "import sys; assert sys.version_info >= (3,10)" >/dev/null 2>&1 \
  && say_ok "Python >= 3.10" || say_err "Python < 3.10"

# Deps
python -c "import yaml" >/dev/null 2>&1 && say_ok "PyYAML installed" || say_err "PyYAML missing"
python -c "import gspread" >/dev/null 2>&1 && say_ok "gspread installed" || say_err "gspread missing"
python -c "import openai" >/dev/null 2>&1 && say_ok "openai installed" || say_err "openai missing"
python -c "import pydantic" >/dev/null 2>&1 && say_ok "pydantic installed" || say_err "pydantic missing"
python -c "import playwright" >/dev/null 2>&1 && say_ok "playwright installed" || say_err "playwright missing"
python -c "import bs4" >/dev/null 2>&1 && say_ok "bs4 installed" || say_err "bs4 missing"
python -c "import readability" >/dev/null 2>&1 && say_ok "readability installed" || say_err "readability missing"

# Runtime files
[[ -f "$CONFIG_PATH" ]] && say_ok "config present: $CONFIG_PATH" || say_err "config missing: $CONFIG_PATH"
[[ -f "$SA_PATH" ]] && say_ok "service account present: $SA_PATH" || say_err "service account missing: $SA_PATH"
[[ -f config/scf-export-content.json ]] && say_ok "SCF export present" || say_warn "SCF export missing (needed for enums)"

# Output dirs
for d in out out_ai out_ai_meta out_meta out_claims out_xaio out_wp locks .runtime .runtime/secrets; do
  [[ -d "$d" ]] && say_ok "dir exists: $d" || say_warn "dir missing: $d"
done

# OpenAI key
if [[ -n "${OPENAI_API_KEY:-}" ]]; then
  say_ok "OPENAI_API_KEY present"
else
  say_err "Missing OPENAI_API_KEY (set it in .runtime/.env)"
fi

# WordPress creds (if you use wp_upload_queue)
if [[ -n "${WP_USERNAME:-}" && -n "${WP_APP_PASSWORD:-}" ]]; then
  say_ok "WP creds present"
else
  say_warn "WP creds not set (WP_USERNAME / WP_APP_PASSWORD) - WP upload stage will fail"
fi

# Brave CDP
if command -v curl >/dev/null 2>&1; then
  if [[ -f "$CONFIG_PATH" ]]; then
    cdp_endpoint="$(python - <<PY
import yaml
from pathlib import Path
cfg = yaml.safe_load(Path("$CONFIG_PATH").read_text(encoding="utf-8"))
endpoint = ((cfg.get("fetch") or {}).get("browser_cdp_endpoint") or "").strip()
print(endpoint)
PY
)"
    if [[ -n "$cdp_endpoint" ]]; then
      probe="${cdp_endpoint%/}/json/version"
      if curl -fsS --max-time 2 "$probe" >/dev/null 2>&1; then
        say_ok "CDP reachable: $probe"
      else
        say_err "CDP not reachable at $probe"
      fi
    else
      say_warn "CDP endpoint not configured (fetch.browser_cdp_endpoint)"
    fi
  else
    say_warn "CDP check skipped (config missing)"
  fi
else
  say_warn "curl not installed (sudo apt install -y curl)"
fi

# Sheets access
if [[ -f "$CONFIG_PATH" && -f "$SA_PATH" ]]; then
  if python - <<PY
import yaml
import gspread
from pathlib import Path

cfg = yaml.safe_load(Path("$CONFIG_PATH").read_text(encoding="utf-8"))
sheet = cfg["sheet"]["spreadsheet_url"]
worksheet = cfg["sheet"]["worksheet_name"]
header_row = int(cfg["sheet"].get("header_row", 1))

gc = gspread.service_account(filename="$SA_PATH")
wks = gc.open_by_url(sheet).worksheet(worksheet)
_ = wks.row_values(header_row)
PY
  then
    say_ok "Sheets reachable (header row read)"
  else
    say_err "Sheets access failed (check credentials, sheet URL, and sharing)"
  fi
else
  say_err "Sheets check skipped (missing config or service account file)"
fi

echo
if [[ "$fail" -eq 0 ]]; then
  echo "Doctor: ✅ no hard failures."
else
  echo "Doctor: ❌ failures found. Fix ERR items above."
  exit 1
fi
