#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$PROJECT_DIR/systemd/user"
DEST_DIR="$HOME/.config/systemd/user"

mkdir -p "$DEST_DIR"

echo "Installing user systemd units..."
shopt -s nullglob
for unit in "$SRC_DIR"/*.service "$SRC_DIR"/*.timer; do
  unit_name="$(basename "$unit")"
  sed "s|%h/xAIO-URL-Agent|$PROJECT_DIR|g" "$unit" > "$DEST_DIR/$unit_name"
  echo "Installed $unit_name"
done
shopt -u nullglob

systemctl --user daemon-reload

echo "Enable timers:"
echo "  systemctl --user enable --now pipeline.timer"
