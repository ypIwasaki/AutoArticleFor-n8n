#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export N8N_RESTRICT_FILE_ACCESS_TO="${N8N_RESTRICT_FILE_ACCESS_TO:-$PROJECT_ROOT/content}"
export PROJECT_ROOT="$PROJECT_ROOT"

echo "N8N_RESTRICT_FILE_ACCESS_TO=$N8N_RESTRICT_FILE_ACCESS_TO"
echo "PROJECT_ROOT=$PROJECT_ROOT"

cd "$HOME"
exec n8n
