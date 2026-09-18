#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v python3 >/dev/null || { echo "Install python3 in Ubuntu first." >&2; exit 1; }
command -v node >/dev/null || { echo "Install Node.js $(cat "$ROOT/.nvmrc") first (see docs/pc-migration.md)." >&2; exit 1; }
EXPECTED="$(cat "$ROOT/.nvmrc")"
[[ "$(node --version)" == "v$EXPECTED" ]] || { echo "Select Node.js $EXPECTED before setup." >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3,8), "Python 3.8+ required"'
command -v npm >/dev/null
NPM_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["npm"])' "$ROOT/config/runtime-versions.json")"
[[ "$(npm --version)" == "$NPM_VERSION" ]] || { echo "Run: npm install -g npm@$NPM_VERSION" >&2; exit 1; }
npm ci --prefix "$ROOT/runtime" --no-audit --no-fund
node "$ROOT/scripts/check_runtime.cjs"
"$ROOT/runtime/node_modules/.bin/n8n" --version
printf '\nRuntime installed. Restore the transfer bundle next; see docs/pc-migration.md.\n'
