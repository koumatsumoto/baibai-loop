#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: jobs/scripts/publish.sh --bucket baibai-stores (--proposal-id ID | --app-only) [--check]\n'
  printf 'Check the App target, pulled version, and optional proposal; without --check, upload and request Web materialization.\n'
}

if [[ $# -eq 1 && ( "$1" == "-h" || "$1" == "--help" ) ]]; then
  usage
  exit 0
fi
invalid() { usage >&2; exit 2; }
bucket="" proposal_id="" app_only=false check_only=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bucket)
      [[ $# -ge 2 && -z "$bucket" && "$2" == "baibai-stores" ]] || invalid
      bucket="$2"; shift 2 ;;
    --proposal-id)
      [[ $# -ge 2 && -z "$proposal_id" && "$2" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]*$ ]] || invalid
      proposal_id="$2"; shift 2 ;;
    --app-only)
      [[ "$app_only" == false ]] || invalid
      app_only=true; shift ;;
    --check)
      [[ "$check_only" == false ]] || invalid
      check_only=true; shift ;;
    *) invalid ;;
  esac
done
[[ -n "$bucket" ]] || invalid
if [[ "$app_only" == true ]]; then [[ -z "$proposal_id" ]] || invalid
else [[ -n "$proposal_id" ]] || invalid
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"
uv run baibai-engine portfolio ledger \
  --db "${repo_root}/stores/application/baibai.sqlite" >/dev/null
args=(push-app --expect-bucket "$bucket")
if [[ -n "$proposal_id" ]]; then args+=(--proposal-id "$proposal_id"); fi
if [[ "$check_only" == true ]]; then args+=(--check); fi
"${repo_root}/jobs/scripts/r2_transfer.sh" "${args[@]}"
if [[ "$check_only" == true ]]; then exit 0; fi
printf 'application DB uploaded\n'
if gh workflow run cloud-materialize.yml --repo koumatsumoto/baibai-loop --ref main; then
  printf 'cloud-materialize dispatch requested\n'
else
  printf 'application DB is already uploaded; cloud-materialize dispatch failed or is uncertain.\n' >&2
  printf 'Do not repeat publish.sh. Check Actions, then retry only gh workflow run cloud-materialize.yml --repo koumatsumoto/baibai-loop --ref main if no run was created.\n' >&2
  exit 1
fi
