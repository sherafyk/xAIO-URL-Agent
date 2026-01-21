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
