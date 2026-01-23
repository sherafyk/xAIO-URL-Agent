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

# Enable what you want (adjust as needed)
systemctl --user enable --now pipeline.timer

echo "Done. Units are symlinked from repo -> $UNIT_DST"
echo "Repo is the source of truth for unit files."
