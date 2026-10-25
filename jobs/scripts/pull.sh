#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
"${repo_root}/jobs/scripts/r2_transfer.sh" pull-machine
"${repo_root}/jobs/scripts/r2_transfer.sh" hydrate-market
printf 'market/runs/market-context stores pulled, validated, and market hydrated\n'
