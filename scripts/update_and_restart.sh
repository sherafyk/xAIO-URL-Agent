#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "Stashing local changes (config/secrets)..."
git stash push -u -m "auto-stash before update" || true

echo "Pulling latest changes..."
git pull --rebase

echo "Ensuring systemd units are installed..."
./scripts/ensure_systemd_user.sh || true

echo "Restarting timers (if installed)..."
systemctl --user daemon-reload || true
systemctl --user restart pipeline.timer 2>/dev/null || true

echo "Done."
