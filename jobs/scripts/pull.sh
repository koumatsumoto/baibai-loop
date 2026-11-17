#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: jobs/scripts/pull.sh\n'
  printf 'Download machine and market-context stores, then hydrate the market.\n'
}

if [[ $# -ne 0 ]]; then
  if [[ $# -eq 1 && ( "$1" == "-h" || "$1" == "--help" ) ]]; then
    usage
    exit 0
  fi
  usage >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
"${repo_root}/jobs/scripts/r2_transfer.sh" pull-machine
"${repo_root}/jobs/scripts/r2_transfer.sh" pull-indicators
"${repo_root}/jobs/scripts/r2_transfer.sh" hydrate-market
printf 'market/runs/market-context stores pulled, validated, and market hydrated\n'
