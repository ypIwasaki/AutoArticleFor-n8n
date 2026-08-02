#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The workflow reads config/keywords.json and writes archives under content/.
export N8N_RESTRICT_FILE_ACCESS_TO="${N8N_RESTRICT_FILE_ACCESS_TO:-$PROJECT_ROOT}"
export PROJECT_ROOT="$PROJECT_ROOT"

# Local workflows use the configured Data Table ID through $env expressions.
export N8N_BLOCK_ENV_ACCESS_IN_NODE="${N8N_BLOCK_ENV_ACCESS_IN_NODE:-false}"

# Keep environment-specific Data Table IDs out of committed workflow exports.
if [[ -f "$PROJECT_ROOT/.env" ]]; then
  table_id="$(grep -m1 "^N8N_ARTICLE_CLASSIFICATIONS_TABLE_ID=" "$PROJECT_ROOT/.env" | cut -d= -f2-)"
  if [[ -n "$table_id" ]]; then
    export N8N_ARTICLE_CLASSIFICATIONS_TABLE_ID="$table_id"
  fi
fi

echo "N8N_RESTRICT_FILE_ACCESS_TO=$N8N_RESTRICT_FILE_ACCESS_TO"
echo "PROJECT_ROOT=$PROJECT_ROOT"

cd "$HOME"
N8N_BIN="${N8N_BIN:-$HOME/.local/bin/n8n}"
if [[ ! -x "$N8N_BIN" ]]; then
  N8N_BIN="$(command -v n8n)"
fi
exec "$N8N_BIN"
