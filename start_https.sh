#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[[ -f .runtime.env ]] && source .runtime.env
export ERROR_ARCHIVER_CONFIG_FILE="${ERROR_ARCHIVER_CONFIG_FILE:-instance/settings.json}"
: "${SSL_CERT_FILE:?Set SSL_CERT_FILE}" "${SSL_KEY_FILE:?Set SSL_KEY_FILE}"
exec .venv/bin/gunicorn --workers 2 --bind "${HOST:-127.0.0.1}:${PORT:-8443}" --certfile "$SSL_CERT_FILE" --keyfile "$SSL_KEY_FILE" 'app:app'
