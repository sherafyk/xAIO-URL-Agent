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
