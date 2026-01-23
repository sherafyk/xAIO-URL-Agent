#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO_DIR/systemd/user"
UNIT_DST="$HOME/.config/systemd/user"

mkdir -p "$UNIT_DST"

# Symlink units so repo is source of truth
for f in "$UNIT_SRC"/*.service "$UNIT_SRC"/*.timer; do
  [ -e "$f" ] || continue
  ln -sf "$f" "$UNIT_DST/$(basename "$f")"
done

systemctl --user daemon-reload

echo "Systemd units symlinked from repo -> $UNIT_DST"

echo
echo "Next steps:"
echo "  1) Make sure your runtime config exists:" 
echo "       $REPO_DIR/.runtime/.env"
echo "       $REPO_DIR/.runtime/config.yaml"
echo "       $REPO_DIR/.runtime/secrets/service_account.json"
echo "     If you haven't created them yet, run:" 
echo "       $REPO_DIR/scripts/init_runtime.sh"
echo

echo "  2) Enable what you want (adjust as needed). Example:" 
echo "       systemctl --user enable --now pipeline.timer"
