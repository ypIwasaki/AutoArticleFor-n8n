#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${TALENT_DASHBOARD_HOST:-127.0.0.1}"
PORT="${TALENT_DASHBOARD_PORT:-8765}"

if curl -fsS "http://${HOST}:${PORT}/api/health" >/dev/null 2>&1; then
  printf 'Talent Index dashboard already running: http://%s:%s\n' "$HOST" "$PORT"
  exit 0
fi

exec python3 "$PROJECT_ROOT/apps/talent-dashboard/server.py" --host "$HOST" --port "$PORT"
