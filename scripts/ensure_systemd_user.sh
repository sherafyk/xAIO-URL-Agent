#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
PIPELINE_UNIT="$UNIT_DIR/pipeline.service"
DEFAULT_REPO="$HOME/xAIO-URL-Agent"

needs_install=0

if [[ ! -f "$PIPELINE_UNIT" ]]; then
  needs_install=1
elif rg -q "%h/xAIO-URL-Agent" "$PIPELINE_UNIT"; then
  if [[ "$PROJECT_DIR" != "$DEFAULT_REPO" ]]; then
    needs_install=1
  fi
elif ! rg -q "$PROJECT_DIR" "$PIPELINE_UNIT"; then
  needs_install=1
fi

if [[ "$needs_install" -eq 1 ]]; then
  "$PROJECT_DIR/scripts/install_systemd_user.sh"
else
  systemctl --user daemon-reload || true
fi

echo "Systemd user units are installed for: $PROJECT_DIR"
