#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: jobs/scripts/publish.sh\n'
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
cd "${repo_root}"
uv run baibai-engine portfolio ledger \
  --db "${repo_root}/stores/application/baibai.sqlite" >/dev/null
"${repo_root}/jobs/scripts/r2_transfer.sh" push-app
printf 'application DB uploaded\n'
if gh workflow run cloud-materialize.yml --ref main; then
  printf 'cloud-materialize dispatch requested\n'
else
  printf 'application DB is already uploaded; cloud-materialize dispatch failed or is uncertain.\n' >&2
  printf 'Do not repeat publish.sh. Check Actions, then retry only gh workflow run cloud-materialize.yml --ref main if no run was created.\n' >&2
  exit 1
fi
