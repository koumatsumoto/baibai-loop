#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
"${repo_root}/batch/scripts/r2_transfer.sh" pull-machine
"${repo_root}/batch/scripts/r2_transfer.sh" hydrate-market
printf 'market/runs/macro stores pulled, validated, and market hydrated\n'
