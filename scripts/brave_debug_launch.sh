#!/usr/bin/env bash
set -euo pipefail

PORT="${BRAVE_CDP_PORT:-9222}"
PROFILE_DIR="${BRAVE_PROFILE_DIR:-$HOME/.config/BraveSoftware/Brave-Browser-Beta-Profile-xaio}"

echo "Launching Brave Beta with CDP on port $PORT"
echo "Profile: $PROFILE_DIR"

mkdir -p "$PROFILE_DIR"

# Try common binary names
if command -v brave-browser-beta >/dev/null 2>&1; then
  BIN="brave-browser-beta"
elif command -v brave-browser >/dev/null 2>&1; then
  BIN="brave-browser"
else
  echo "ERROR: Brave not found. Install Brave or update the script to your binary."
  exit 1
fi

exec "$BIN" --remote-debugging-port="$PORT" --user-data-dir="$PROFILE_DIR"
