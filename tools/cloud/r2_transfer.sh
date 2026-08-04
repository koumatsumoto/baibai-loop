#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
stores_bucket="${R2_STORES_BUCKET:-baibai-stores}"
serving_bucket="${R2_SERVING_BUCKET:-baibai-serving}"
copy_read_timeout=300
transfer_staging=""

cleanup_staging() {
  if [[ -n "${transfer_staging}" && -d "${transfer_staging}" ]]; then
    case "${transfer_staging}" in
      "${repo_root}"/.r2-transfer.*) rm -r -- "${transfer_staging}" ;;
      *) printf 'refusing to remove unexpected staging path: %s\n' "${transfer_staging}" >&2 ;;
    esac
  fi
}

trap cleanup_staging EXIT

load_credentials() {
  if [[ -z "${R2_ACCOUNT_ID:-}" || -z "${R2_ACCESS_KEY_ID:-}" || -z "${R2_SECRET_ACCESS_KEY:-}" ]]; then
    if [[ -f "${repo_root}/.env" ]]; then
      set -a
      # shellcheck disable=SC1091
      source "${repo_root}/.env"
      set +a
    fi
  fi
  : "${R2_ACCOUNT_ID:?R2_ACCOUNT_ID is required}"
  : "${R2_ACCESS_KEY_ID:?R2_ACCESS_KEY_ID is required}"
  : "${R2_SECRET_ACCESS_KEY:?R2_SECRET_ACCESS_KEY is required}"
  export AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}"
  export AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}"
  export AWS_DEFAULT_REGION=auto
  export AWS_EC2_METADATA_DISABLED=true
  endpoint="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
}

aws_s3() {
  aws s3 "$@" --endpoint-url "${endpoint}" --only-show-errors --no-progress
}

remote_object_exists() {
  # Exact-key existence, so a sibling object (a `.bak`) never reads as the key
  # itself the way a prefix listing would. `aws s3 ls` is not usable here: it
  # rejects the transfer flags `aws_s3` passes and would fail for every key.
  aws s3api head-object \
    --bucket "${stores_bucket}" \
    --key "$1" \
    --endpoint-url "${endpoint}" \
    >/dev/null 2>&1
}

store_path() {
  case "$1" in
    market.sqlite) printf '%s/data/screening/market.sqlite\n' "${repo_root}" ;;
    runs.sqlite) printf '%s/data/screening/runs.sqlite\n' "${repo_root}" ;;
    macro.sqlite) printf '%s/data/indicators/macro.sqlite\n' "${repo_root}" ;;
    baibai.sqlite) printf '%s/data/app/baibai.sqlite\n' "${repo_root}" ;;
    *) printf 'unknown store key: %s\n' "$1" >&2; return 2 ;;
  esac
}

check_sqlite() {
  UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
    uv run python "${repo_root}/tools/cloud/sqlite_snapshot.py" check --path "$1"
}

check_sqlite_schema() {
  UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
    uv run python "${repo_root}/tools/cloud/sqlite_snapshot.py" check \
      --path "$1" --schema-version "$2"
}

sqlite_schema_version() {
  UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
    uv run python "${repo_root}/tools/cloud/sqlite_snapshot.py" version --path "$1"
}

snapshot_sqlite() {
  UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
    uv run python "${repo_root}/tools/cloud/sqlite_snapshot.py" create \
      --source "$1" --output "$2"
}

# The merges share their core, so they are imported as modules rather than run as
# scripts: a script run puts its own directory on the path instead of the repository
# root, and the shared module is then unreachable.
merge_store() {
  local module="$1"
  shift
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m "${module}" --source "$1" --target "$2"
  )
}

merge_indicator_store() {
  merge_store tools.cloud.merge_indicator_store "$1" "$2"
}

merge_market_store() {
  merge_store tools.cloud.merge_market_store "$1" "$2"
}

pull_keys() {
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  local key target
  for key in "$@"; do
    aws_s3 cp "s3://${stores_bucket}/${key}" "${transfer_staging}/${key}"
    check_sqlite "${transfer_staging}/${key}"
  done
  for key in "$@"; do
    target="$(store_path "${key}")"
    mkdir -p "$(dirname "${target}")"
    mv -f "${transfer_staging}/${key}" "${target}"
  done
  cleanup_staging
  transfer_staging=""
}

backup_remote_key() {
  # Keep one generation of the object being replaced. A store is rebuildable from
  # its sources in principle, but some of it is not re-fetchable in practice (the
  # PMI history depends on release URLs the publisher eventually drops), so an
  # overwrite by a damaged or wrongly pruned snapshot must stay recoverable. R2
  # copies server-side, so this costs a request and no transfer.
  # The copy goes through CopyObject directly because `aws s3 cp` switches implementation
  # by object size and R2 rejects both branches: a multipart copy asks for the source tags
  # through GetObjectTagging, and a single-part one sends x-amz-tagging-directive, neither
  # of which R2 implements. CopyObject sends no directive, is one server-side request with
  # no transfer, and covers objects up to 5GB (the largest store here is well inside that).
  # R2 answers CopyObject only once the copy is finished, and that wait grows with the
  # object size: the several-hundred-MB stores do not fit the CLI's 60s default read
  # timeout, which surfaces as `Read timeout on endpoint URL` and aborts the whole push.
  # The wider limit below is a ceiling, not a delay — retries stay at the CLI default so a
  # copy that is genuinely stuck still fails the step inside the job's time budget.
  local key="$1"
  if remote_object_exists "${key}"; then
    aws s3api copy-object \
      --bucket "${stores_bucket}" \
      --key "${key}.bak" \
      --copy-source "${stores_bucket}/${key}" \
      --endpoint-url "${endpoint}" \
      --cli-read-timeout "${copy_read_timeout}" \
      >/dev/null
  fi
}

push_keys() {
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  local key source
  for key in "$@"; do
    source="$(store_path "${key}")"
    snapshot_sqlite "${source}" "${transfer_staging}/${key}"
  done
  for key in "$@"; do
    backup_remote_key "${key}"
    aws_s3 cp "${transfer_staging}/${key}" "s3://${stores_bucket}/${key}"
  done
  cleanup_staging
  transfer_staging=""
}

seed_keys() {
  local key
  for key in "$@"; do
    if remote_object_exists "${key}"; then
      printf 'refusing initial seed: s3://%s/%s already exists\n' \
        "${stores_bucket}" "${key}" >&2
      return 2
    fi
  done
  push_keys "$@"
}

preserve_market_v13() {
  # v14 rebuilds EDINET document rows and cannot reconstruct the discarded shape.
  # This fixed key is write-once: later runs verify it instead of replacing it.
  local source backup_key version uploaded_snapshot verified_snapshot
  source="$(store_path market.sqlite)"
  backup_key="schema-migrations/market-v13.sqlite"
  version="$(sqlite_schema_version "${source}")"
  if [[ ! "${version}" =~ ^[0-9]+$ ]] || (( version < 13 )); then
    printf 'market schema preflight expected v13 or newer, got v%s\n' "${version}" >&2
    return 2
  fi
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  uploaded_snapshot="${transfer_staging}/market-v13-upload.sqlite"
  verified_snapshot="${transfer_staging}/market-v13-verify.sqlite"
  if ! remote_object_exists "${backup_key}"; then
    if [[ "${version}" != "13" ]]; then
      printf 'required immutable rollback object is missing: s3://%s/%s\n' \
        "${stores_bucket}" "${backup_key}" >&2
      return 2
    fi
    snapshot_sqlite "${source}" "${uploaded_snapshot}"
    aws_s3 cp "${uploaded_snapshot}" "s3://${stores_bucket}/${backup_key}"
  fi
  aws_s3 cp "s3://${stores_bucket}/${backup_key}" "${verified_snapshot}"
  if [[ ! -s "${verified_snapshot}" ]]; then
    printf 'rollback object is empty: s3://%s/%s\n' "${stores_bucket}" "${backup_key}" >&2
    return 2
  fi
  check_sqlite_schema "${verified_snapshot}" 13
  if [[ -f "${uploaded_snapshot}" ]] && ! cmp -s "${uploaded_snapshot}" "${verified_snapshot}"; then
    printf 'rollback object differs from the uploaded snapshot: s3://%s/%s\n' \
      "${stores_bucket}" "${backup_key}" >&2
    return 2
  fi
  cleanup_staging
  transfer_staging=""
}

download_market_v13_rollback() {
  local output="$1"
  local backup_key="schema-migrations/market-v13.sqlite"
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  local verified_snapshot="${transfer_staging}/market-v13.sqlite"
  aws_s3 cp "s3://${stores_bucket}/${backup_key}" "${verified_snapshot}"
  if [[ ! -s "${verified_snapshot}" ]]; then
    printf 'rollback object is empty: s3://%s/%s\n' "${stores_bucket}" "${backup_key}" >&2
    return 2
  fi
  check_sqlite_schema "${verified_snapshot}" 13
  mkdir -p "$(dirname "${output}")"
  if [[ -e "${output}" ]]; then
    printf 'refusing rollback download overwrite: %s\n' "${output}" >&2
    return 2
  fi
  mv "${verified_snapshot}" "${output}"
  cleanup_staging
  transfer_staging=""
}

upload_serving() {
  local output_dir="$1"
  if [[ ! -f "${output_dir}/views/meta.json" ]]; then
    printf 'serving export is incomplete: %s/views/meta.json is missing\n' "${output_dir}" >&2
    return 2
  fi
  aws_s3 sync "${output_dir}/views/" "s3://${serving_bucket}/views/" \
    --delete --exclude meta.json
  if [[ -d "${output_dir}/history/candidate-views" ]]; then
    aws_s3 sync "${output_dir}/history/candidate-views/" \
      "s3://${serving_bucket}/history/candidate-views/"
  fi
  if [[ -d "${output_dir}/history/longlists" ]]; then
    aws_s3 sync "${output_dir}/history/longlists/" \
      "s3://${serving_bucket}/history/longlists/"
  fi
  # Freshness is published only after every view and history upload succeeds.
  aws_s3 cp "${output_dir}/views/meta.json" "s3://${serving_bucket}/views/meta.json"
}

pull_app() {
  # application DB の正本はローカルにある (判断はローカルで publish し、`push-app` で
  # cloud へ出す)。cloud copy で置換してよいのは「ローカルに正本が無い」ときだけなので、
  # ファイルがあれば止める。publish 済みで未 push の判断は再生成できず、上書きすると
  # 復元手段が無い。CI は checkout 直後で data/app/ が空なので素通りする。
  local target
  target="$(store_path baibai.sqlite)"
  if [[ -e "${target}" ]]; then
    printf 'refusing application store download overwrite: %s\n' "${target}" >&2
    return 2
  fi
  pull_keys baibai.sqlite
}

pull_longlist_history() {
  local output_dir="$1"
  if [[ -e "${output_dir}" ]]; then
    printf 'refusing longlist history download overwrite: %s\n' "${output_dir}" >&2
    return 2
  fi
  mkdir -p "${output_dir}"
  aws_s3 sync "s3://${serving_bucket}/history/longlists/" "${output_dir}/"
}

upload_run_summary() {
  # The workflow run summary lives outside `views/`, which `upload_serving`
  # mirrors with `--delete`: a failed run publishes no export, so its record has
  # to survive the next successful one. One object, always overwritten — the run
  # timeline stays in Discord and the Actions history.
  local summary_path="$1"
  if [[ ! -f "${summary_path}" ]]; then
    printf 'no workflow run summary to upload: %s\n' "${summary_path}" >&2
    return 2
  fi
  aws_s3 cp "${summary_path}" "s3://${serving_bucket}/system/latest-run.json"
}

usage() {
  printf 'usage: %s {pull-machine|pull-app|pull-market|pull-longlist-history DIR|preserve-market-v13|download-market-v13-rollback FILE|seed-all|push-machine|push-market|push-macro|push-app|upload-serving DIR|upload-run-summary FILE}\n' "$0" >&2
}

load_credentials
case "${1:-}" in
  pull-app)
    pull_app
    ;;
  pull-machine)
    pull_keys market.sqlite runs.sqlite macro.sqlite
    ;;
  # A pass that only writes the market store round-trips the other two for nothing,
  # and pushing them back unchanged after hours would revert whatever else wrote them
  # meanwhile. These two move the market store alone.
  pull-market)
    pull_keys market.sqlite
    ;;
  pull-longlist-history)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    pull_longlist_history "$2"
    ;;
  preserve-market-v13)
    preserve_market_v13
    ;;
  download-market-v13-rollback)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    download_market_v13_rollback "$2"
    ;;
  seed-all)
    seed_keys market.sqlite runs.sqlite macro.sqlite baibai.sqlite
    ;;
  push-machine)
    if [[ "${GITHUB_ACTIONS:-}" != "true" ]]; then
      printf 'refusing machine-store push outside GitHub Actions\n' >&2
      exit 2
    fi
    push_keys market.sqlite runs.sqlite macro.sqlite
    ;;
  push-market)
    # Deep history is fetched where there is time for it — hours of provider calls for a
    # decade of bars — while the daily batch keeps adding recent days the deep copy has
    # never seen. So the local store is only publishable once it contains the cloud copy:
    # pull it, merge it in, and let the merge refuse the upload if any cloud row would be
    # left behind. That check is what makes this safe to run outside GitHub Actions,
    # where an unconditional upload would roll the daily batch back.
    transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
    aws_s3 cp "s3://${stores_bucket}/market.sqlite" "${transfer_staging}/market.sqlite"
    check_sqlite "${transfer_staging}/market.sqlite"
    merge_market_store "${transfer_staging}/market.sqlite" "$(store_path market.sqlite)"
    cleanup_staging
    transfer_staging=""
    push_keys market.sqlite
    ;;
  push-macro)
    # Deep history is fetched locally with `macro refresh --all-history`, which the
    # cloud's rolling-window refresh never reaches, while the daily batch keeps adding
    # recent observations the local store has never seen. So the local store is only
    # publishable once it contains the cloud copy: pull it, merge it in, and let the
    # merge refuse the upload if any cloud row would be left behind.
    transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
    aws_s3 cp "s3://${stores_bucket}/macro.sqlite" "${transfer_staging}/macro.sqlite"
    check_sqlite "${transfer_staging}/macro.sqlite"
    merge_indicator_store "${transfer_staging}/macro.sqlite" "$(store_path macro.sqlite)"
    cleanup_staging
    transfer_staging=""
    push_keys macro.sqlite
    ;;
  push-app)
    push_keys baibai.sqlite
    ;;
  # The serving bucket has one writer: the batch that produces a complete export.
  # `upload_serving` mirrors `views/` with `--delete`, so a partial local export would
  # remove production views, and the local credential carries write permission because
  # R2 grants it per token rather than per bucket. The boundary lives here instead.
  upload-serving)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    if [[ "${GITHUB_ACTIONS:-}" != "true" ]]; then
      printf 'refusing serving upload outside GitHub Actions\n' >&2
      exit 2
    fi
    upload_serving "$2"
    ;;
  upload-run-summary)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    if [[ "${GITHUB_ACTIONS:-}" != "true" ]]; then
      printf 'refusing run-summary upload outside GitHub Actions\n' >&2
      exit 2
    fi
    upload_run_summary "$2"
    ;;
  *)
    usage
    exit 2
    ;;
esac
