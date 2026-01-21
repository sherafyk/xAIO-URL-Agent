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
