#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: batch/scripts/publish.sh\n'
  printf 'Upload the application DB and dispatch cloud-materialize.\n'
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
uv run baibai-engine position ledger \
  --db "${repo_root}/stores/application/baibai.sqlite" >/dev/null
"${repo_root}/batch/scripts/r2_transfer.sh" push-app
gh workflow run cloud-materialize.yml --ref main
printf 'application DB uploaded; cloud-materialize dispatch requested\n'
