#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[[ -f .runtime.env ]] && source .runtime.env
export ERROR_ARCHIVER_CONFIG_FILE="${ERROR_ARCHIVER_CONFIG_FILE:-instance/settings.json}"
exec .venv/bin/gunicorn --workers 2 --bind "${HOST:-127.0.0.1}:${PORT:-8080}" 'app:app'
