#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO_DIR/systemd/user"
UNIT_DST="$HOME/.config/systemd/user"
USER_ENV="$HOME/.config/xaio-url-agent.env"

mkdir -p "$UNIT_DST"

# Symlink units so repo is source of truth
for f in "$UNIT_SRC"/*.service "$UNIT_SRC"/*.timer; do
  [ -e "$f" ] || continue
  ln -sf "$f" "$UNIT_DST/$(basename "$f")"
done

systemctl --user daemon-reload

# Provide a stable override for the repo path if the default %h/xAIO-URL-Agent
# does not match the actual repo location.
if [ ! -f "$USER_ENV" ]; then
  cat >"$USER_ENV" <<EOF
# Optional overrides for xAIO systemd units.
XAIO_PROJECT_DIR="$REPO_DIR"
XAIO_CONFIG_PATH="$REPO_DIR/config.yaml"
EOF
fi

# Enable what you want (adjust as needed)
systemctl --user enable --now pipeline.timer

echo "Done. Units are symlinked from repo -> $UNIT_DST"
echo "Repo is the source of truth for unit files."
