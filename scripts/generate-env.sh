#!/usr/bin/env bash
# Fills .env.example's blank secrets and writes .env (which is gitignored).
# Run this locally; paste the result into Dokploy's Environment tab.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "$here/.env" ]]; then
  echo "refusing to overwrite an existing .env" >&2
  exit 1
fi

sed \
  -e "s|^WC_API_TOKEN=$|WC_API_TOKEN=$(openssl rand -hex 24)|" \
  -e "s|^POSTGRES_PASSWORD=$|POSTGRES_PASSWORD=$(openssl rand -hex 16)|" \
  "$here/.env.example" > "$here/.env"

echo "wrote $here/.env"
echo
echo "Paste these into Dokploy → Environment:"
echo "---------------------------------------"
grep -v '^#' "$here/.env" | grep -v '^$'
