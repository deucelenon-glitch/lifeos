#!/usr/bin/env bash
# Run LIFEOS server
cd "$(dirname "$0")/.."
source venv/bin/activate || true
exec uvicorn run:app --host 0.0.0.0 --port 8080 "$@"