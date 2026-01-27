#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RT="$REPO_DIR/.runtime"

mkdir -p "$RT" "$RT/secrets" "$RT/logs" "$REPO_DIR/locks" "$REPO_DIR/out" "$REPO_DIR/out_ai" \
  "$REPO_DIR/out_ai_meta" "$REPO_DIR/out_meta" "$REPO_DIR/out_claims" "$REPO_DIR/out_xaio" "$REPO_DIR/out_wp"

# NEW: buffered panel analysis output
mkdir -p "$REPO_DIR/out_buffers"

if [ ! -f "$RT/.env" ]; then
  if [ -f "$REPO_DIR/.env.example" ]; then
    cp "$REPO_DIR/.env.example" "$RT/.env"
    echo "Created $RT/.env (copied from .env.example)"
  else
    touch "$RT/.env"
    echo "Created empty $RT/.env"
  fi
else
  echo "Exists: $RT/.env"
fi

if [ ! -f "$RT/config.yaml" ]; then
  if [ -f "$REPO_DIR/config/config.example.yaml" ]; then
    cp "$REPO_DIR/config/config.example.yaml" "$RT/config.yaml"
    echo "Created $RT/config.yaml (copied from config/config.example.yaml)"
  else
    echo "WARNING: missing config/config.example.yaml; you must create $RT/config.yaml manually" >&2
  fi
else
  echo "Exists: $RT/config.yaml"
fi

if [ ! -f "$RT/secrets/service_account.json" ]; then
  cat > "$RT/secrets/service_account.json" <<'JSON'
{
  "type": "service_account",
  "project_id": "REPLACE_ME",
  "private_key_id": "REPLACE_ME",
  "private_key": "-----BEGIN PRIVATE KEY-----\nREPLACE_ME\n-----END PRIVATE KEY-----\n",
  "client_email": "REPLACE_ME",
  "client_id": "REPLACE_ME",
  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
  "token_uri": "https://oauth2.googleapis.com/token",
  "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
  "client_x509_cert_url": "REPLACE_ME"
}
JSON
  echo "Created placeholder $RT/secrets/service_account.json (REPLACE_ME values)."
  echo "Replace it with your real service account JSON."
else
  echo "Exists: $RT/secrets/service_account.json"
fi

echo
echo "Runtime initialized. Next:" 
echo "  - Edit: $RT/.env" 
echo "  - Edit: $RT/config.yaml" 
echo "  - Replace: $RT/secrets/service_account.json" 
