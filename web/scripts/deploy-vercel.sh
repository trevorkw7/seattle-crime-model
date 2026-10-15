#!/usr/bin/env bash
# Build the static dashboard with the current export in public/data and deploy it to Vercel.
#   python -m seacrime.export            # refresh public/data first
#   web/scripts/deploy-vercel.sh [--prod]
# Auth: `vercel login` (device flow) or VERCEL_TOKEN in the environment.
set -euo pipefail
cd "$(dirname "$0")/.."
VERCEL="npx -y vercel@64.1.0"
test -f public/data/meta.json || { echo "public/data/meta.json missing: run python -m seacrime.export first" >&2; exit 1; }
npm run build
cp vercel.static.json out/vercel.json
TOKEN_ARGS=()
if [[ -n "${VERCEL_TOKEN:-}" ]]; then TOKEN_ARGS=(--token "$VERCEL_TOKEN"); fi
# Node's built-in fetch only uses HTTPS_PROXY when told to (needed behind proxies).
NODE_USE_ENV_PROXY=1 $VERCEL deploy out --yes --name "${VERCEL_PROJECT:-seattle-crime-forecast}" "${TOKEN_ARGS[@]}" "$@"
