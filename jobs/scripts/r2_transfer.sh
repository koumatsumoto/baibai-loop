#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
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
      # Fill missing R2 settings from the trusted file; never replace the caller's
      # account, key or target while completing another missing credential.
      local name
      local -a supplied=()
      for name in R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY \
        R2_STORES_BUCKET R2_SERVING_BUCKET R2_GENERATION_DIR R2_LAKE_MIRROR; do
        if [[ -n "${!name:-}" ]]; then supplied+=("$name=${!name}"); fi
      done
      set -a
      # shellcheck disable=SC1091
      source "${repo_root}/.env"
      set +a
      if [[ -n "${supplied[0]:-}" ]]; then export "${supplied[@]}"; fi
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

store_path() {
  case "$1" in
    market.sqlite) printf '%s/stores/market/market.sqlite\n' "${repo_root}" ;;
    runs.sqlite) printf '%s/stores/screening/runs.sqlite\n' "${repo_root}" ;;
    indicators.sqlite) printf '%s/stores/market_context/indicators.sqlite\n' "${repo_root}" ;;
    baibai.sqlite) printf '%s/stores/application/baibai.sqlite\n' "${repo_root}" ;;
    *) printf 'unknown store key: %s\n' "$1" >&2; return 2 ;;
  esac
}

check_sqlite() {
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m baibai_engine.storage.sqlite.snapshot check --path "$1"
  )
}

snapshot_sqlite() {
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m baibai_engine.storage.sqlite.snapshot create \
        --source "$1" --output "$2"
  )
}

merge_economic_store() {
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m baibai_jobs.integration.native_source_merge \
        --canonical "$1" --target "$2" "${@:3}"
  )
}

pull_machine() {
  local mode="$1"
  local -a options=()
  if [[ "${mode}" != portfolio ]]; then
    options+=(--runs "$(store_path runs.sqlite)")
  fi
  if [[ "${mode}" == daily ]]; then
    options+=(--lease-handle "${PUBLICATION_LEASE_HANDLE}")
  fi
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m baibai_jobs.publication.machine_pull pull --mode "${mode}" \
        --market "$(store_path market.sqlite)" --mirror "${lake_mirror}" \
        --bucket "${stores_bucket}" "${options[@]}"
  )
}

hydrate_daily_market() {
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m baibai_jobs.publication.machine_pull hydrate-daily \
        --market "$(store_path market.sqlite)" --mirror "${lake_mirror}" \
        --bucket "${stores_bucket}" --lease-handle "${PUBLICATION_LEASE_HANDLE}"
  )
}

verify_publication_lease() {
  (
    cd "${repo_root}" || exit 1
    R2_STORES_BUCKET="${stores_bucket}" UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m baibai_jobs.publication.lease verify \
        --handle "${PUBLICATION_LEASE_HANDLE:?publication lease handle is required}" >/dev/null
  )
}

remote_version() {
  # ETag identifies the stored object, so a push during the pull changes it.
  aws s3api head-object \
    --bucket "${stores_bucket}" \
    --key "$1" \
    --endpoint-url "${endpoint}" \
    --query ETag \
    --output text
}

generation_path() {
  # Store keys pass through store_path's fixed allowlist before they reach this helper.
  printf '%s/%s.etag\n' "${generation_dir}" "$1"
}

clear_pulled_versions() {
  local key
  for key in "$@"; do
    store_path "${key}" >/dev/null
    rm -f -- "$(generation_path "${key}")"
  done
}

record_pulled_version() {
  local key="$1" version="$2" path temporary
  store_path "${key}" >/dev/null
  mkdir -p "${generation_dir}"
  path="$(generation_path "${key}")"
  temporary="$(mktemp "${generation_dir}/.${key}.XXXXXX")"
  printf '%s\n' "${version}" > "${temporary}"
  mv -f "${temporary}" "${path}"
}

pulled_version() {
  local key="$1" path version
  store_path "${key}" >/dev/null
  path="$(generation_path "${key}")"
  if [[ ! -s "${path}" ]]; then
    printf 'no pulled R2 generation recorded for %s; pull it again before pushing\n' \
      "${key}" >&2
    return 1
  fi
  version="$(<"${path}")"
  if [[ -z "${version}" ]]; then
    printf 'empty pulled R2 generation recorded for %s\n' "${key}" >&2
    return 1
  fi
  printf '%s\n' "${version}"
}

etag_body() {
  # AWS's text query may return a quoted ETag or a null sentinel. Only an actual
  # version can identify an application object generation.
  local value="$1" original="$1"
  value="${value%\"}"
  value="${value#\"}"
  if [[ ! "${value}" =~ ^[A-Za-z0-9._:-]+$ || "${value}" == None || "${value}" == null ]] \
    || [[ "${original}" != "${value}" && "${original}" != "\"${value}\"" ]]; then
    printf 'unusable object version (details withheld)\n' >&2
    return 1
  fi
  printf '%s\n' "${value}"
}

download_indicator_snapshot() {
  local body="$1" expected response version length
  expected="$(etag_body "$2")" || return
  # The bytes used by the pull or merge must come from the selected generation.
  # HEADs around an unpinned copy can both see A even if the GET served B.
  if ! response="$(aws s3api get-object --bucket "${stores_bucket}" --key indicators.sqlite \
    --if-match "\"${expected}\"" --endpoint-url "${endpoint}" \
    --query '[ETag, ContentLength]' --output text "${body}" 2>/dev/null)"; then
    printf 'indicators store download failed (details withheld)\n' >&2
    return 1
  fi
  IFS=$'\t' read -r version length <<< "${response}"
  if [[ "${response}" != "${version}"$'\t'"${length}" || ! "${length}" =~ ^[1-9][0-9]*$ ]] \
    || [[ ! -f "${body}" || "$(wc -c < "${body}")" != "${length}" ]]; then
    printf 'indicators store download length or metadata differs (details withheld)\n' >&2
    return 1
  fi
  version="$(etag_body "${version}")" || return
  if [[ "${version}" != "${expected}" ]]; then
    printf 'indicators store download did not return the requested generation\n' >&2
    return 1
  fi
}

pull_indicators() {
  local key=indicators.sqlite target version
  target="$(store_path "${key}")"
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  version="$(remote_version "${key}")"
  download_indicator_snapshot "${transfer_staging}/${key}" "${version}"
  check_sqlite "${transfer_staging}/${key}"
  if [[ "$(remote_version "${key}")" != "${version}" ]]; then
    printf 'refusing to replace local stores: %s changed on R2 during the pull. ' "${key}" >&2
    printf 'Wait for the daily batch to finish and pull again.\n' >&2
    exit 1
  fi
  mkdir -p "$(dirname "${target}")"
  mv -f "${transfer_staging}/${key}" "${target}"
  cleanup_staging
  transfer_staging=""
}

push_key_if_version() {
  local key="$1" expected_version="$2" current_version
  local body started snapshot_seconds uploaded_version
  current_version="$(remote_version "${key}")"
  if [[ "${current_version}" != "${expected_version}" ]]; then
    printf 'refusing store push: %s changed on R2 during the merge. ' "${key}" >&2
    printf 'Merge the latest cloud copy and try again.\n' >&2
    return 1
  fi
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  body="${transfer_staging}/${key}"
  started="${SECONDS}"
  snapshot_sqlite "$(store_path "${key}")" "${body}"
  snapshot_seconds="$((SECONDS - started))"
  started="${SECONDS}"
  if [[ "${key}" == "indicators.sqlite" ]]; then verify_publication_lease; fi
  # The precheck avoids a snapshot after an already visible race. Only this PUT's
  # If-Match closes the race through the snapshot and upload.
  uploaded_version="$(aws s3api put-object \
    --bucket "${stores_bucket}" \
    --key "${key}" \
    --body "${body}" \
    --if-match "${expected_version}" \
    --endpoint-url "${endpoint}" \
    --query ETag --output text)"
  if [[ "${key}" == "baibai.sqlite" ]]; then
    # A later HEAD could observe a different writer and authorize a stale copy.
    clear_pulled_versions "${key}"
    if [[ -z "${uploaded_version}" || "${uploaded_version}" == "None" ]]; then
      printf 'application uploaded but no ETag returned; pull before next update\n' >&2
      return 1
    fi
    record_pulled_version "${key}" "${uploaded_version}"
  fi
  printf 'store push: key=%s bytes=%s snapshot=%ss upload=%ss\n' \
    "${key}" "$(wc -c < "${body}" | tr -d ' \n')" "${snapshot_seconds}" "$((SECONDS - started))"
  cleanup_staging
  transfer_staging=""
}

pull_app() {
  # application DB の正本はR2にある (判断は取得した作業copyで publish し、`push-app` で
  # cloud へ反映する)。未公開変更を失わないよう、既存の作業copyは置換しない。
  # ファイルがあれば止める。publish 済みで未 push の判断は再生成できず、上書きすると
  # 復元手段が無い。CI は checkout 直後で stores/application/ が空なので素通りする。
  local key=baibai.sqlite target body response version length current pull_fd
  target="$(store_path "${key}")"
  # Another pull must not retire our marker after our App and marker are installed.
  # Lock the stable local directory, not either file; process exit releases the lock.
  mkdir -p "${target%/*}"
  exec {pull_fd}<"${target%/*}"
  if ! flock -n "${pull_fd}"; then
    printf 'cannot lock application work directory for pull; existing copy and origin left alone\n' >&2
    return 2
  fi
  if [[ -e "${target}" || -L "${target}" ]]; then
    printf 'refusing application store download overwrite: %s\n' "${target}" >&2
    return 2
  fi
  clear_pulled_versions "${key}"
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  body="${transfer_staging}/${key}"
  # The same GET must identify the bytes we install. Matching HEADs around an
  # unpinned copy can miss an intervening version if the original is restored.
  if ! response="$(aws s3api get-object --bucket "${stores_bucket}" --key "${key}" \
    --endpoint-url "${endpoint}" --query '[ETag, ContentLength]' --output text "${body}" 2>/dev/null)"; then
    printf 'application store download failed (details withheld)\n' >&2
    return 1
  fi
  IFS=$'\t' read -r version length <<< "${response}"
  if [[ "${response}" != "${version}"$'\t'"${length}" || ! "${length}" =~ ^[1-9][0-9]*$ ]] \
    || [[ ! -f "${body}" || "$(wc -c < "${body}")" != "${length}" ]]; then
    printf 'application store download length or metadata differs (details withheld)\n' >&2
    return 1
  fi
  etag_body "${version}" >/dev/null || return
  check_sqlite "${body}"
  current="$(remote_version "${key}" 2>/dev/null)" || {
    printf 'application store generation could not be checked (details withheld)\n' >&2
    return 1
  }
  if [[ "${current}" != "${version}" ]]; then
    printf 'refusing to replace local stores: %s changed on R2 during the pull. Pull again.\n' "${key}" >&2
    return 1
  fi
  # A concurrent local author may have created the target during the download.
  # Link create-only; -T also refuses a directory that appeared at the target.
  if ! ln -T -- "${body}" "${target}"; then
    printf 'refusing application store download overwrite: %s\n' "${target}" >&2
    return 2
  fi
  record_pulled_version "${key}" "${version}"
  cleanup_staging
  transfer_staging=""
}

# Only refresh the current views; a serving upload does not clean up other keys.
upload_portfolio_views() {
  local directory="$1" name view_file
  local -a names=(portfolio-dashboard outcomes latest-proposal proposal-history latest-us-proposal)
  for name in "${names[@]}"; do
    view_file="${directory}/views/${name}.json"
    [[ -f "${view_file}" && -s "${view_file}" ]] || {
      printf 'missing portfolio view: %s\n' "${name}" >&2
      return 2
    }
  done
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  for name in "${names[@]}"; do
    verify_publication_lease
    if ! aws_s3 cp "${directory}/views/${name}.json" \
      "s3://${R2_SERVING_BUCKET:-baibai-serving}/views/${name}.json" \
      --content-type application/json --cache-control no-store \
      >> "${transfer_staging}/transfer.log" 2>&1; then
      printf 'portfolio view upload failed: %s\n' "${name}" >&2
      return 1
    fi
  done
  # Verify the actual serving bytes while the caller still holds its publication
  # lease. Never print the payload or a diff: these views contain private assets.
  for name in "${names[@]}"; do
    if ! aws_s3 cp "s3://${R2_SERVING_BUCKET:-baibai-serving}/views/${name}.json" \
      "${transfer_staging}/${name}.json" >> "${transfer_staging}/transfer.log" 2>&1; then
      printf 'portfolio view readback failed: %s\n' "${name}" >&2
      return 1
    fi
    if ! cmp -s "${directory}/views/${name}.json" "${transfer_staging}/${name}.json"; then
      printf 'portfolio view readback mismatch: %s\n' "${name}" >&2
      return 1
    fi
  done
  cleanup_staging
  transfer_staging=""
  verify_publication_lease
  printf 'portfolio views verified from remote readback: %s\n' "${#names[@]}"
}

usage() {
  printf 'usage: %s {pull-machine [--daily-preflight]|hydrate-market-for-daily|pull-portfolio-market|pull-app|pull-indicators|push-indicators [--recheck-source SOURCE]...|push-app|upload-portfolio-views DIR}\n' "$0" >&2
}

validate_command() {
  local command="${1:-}"
  case "$command" in
    push-indicators)
      shift
      while [[ $# -gt 0 ]]; do
        [[ $# -ge 2 && "$1" == "--recheck-source" && -n "$2" && "$2" != -* ]] || return 1
        shift 2
      done
      ;;
    upload-portfolio-views)
      [[ $# -eq 2 && -n "$2" && "$2" != -* ]]
      ;;
    pull-machine)
      [[ $# -eq 1 || ( $# -eq 2 && "$2" == "--daily-preflight" && -n "${PUBLICATION_LEASE_HANDLE:-}" ) ]]
      ;;
    hydrate-market-for-daily)
      [[ $# -eq 1 && -n "${PUBLICATION_LEASE_HANDLE:-}" ]]
      ;;
    pull-app|pull-portfolio-market|pull-indicators|push-app)
      [[ $# -eq 1 ]]
      ;;
    *)
      return 1
      ;;
  esac
}

# A mistyped or removed command must stop before helpers or credentials.
validate_command "$@" || { usage; exit 2; }
(cd "${repo_root}" && uv run python -m baibai_jobs.observability.repository_layout)
load_credentials
stores_bucket="${R2_STORES_BUCKET:-baibai-stores}"
generation_dir="${R2_GENERATION_DIR:-${repo_root}/stores/.r2-generations}"
# The lake mirror is a fetch-through cache of immutable objects, so it is named next to
# the stores rather than inside one: every key it holds begins with `lake/`, which is why
# the mirror root is the directory the stores live in and not one of them.
lake_mirror="${R2_LAKE_MIRROR:-${repo_root}/stores}"
case "${1:-}" in
  pull-app)
    pull_app
    ;;
  pull-machine)
    if [[ "${2:-}" == --daily-preflight ]]; then
      pull_machine daily
    else
      pull_machine full
    fi
    ;;
  hydrate-market-for-daily)
    hydrate_daily_market
    ;;
  pull-portfolio-market)
    pull_machine portfolio
    ;;
  pull-indicators)
    pull_indicators
    ;;
  push-indicators)
    shift
    verify_publication_lease
    # Compare local receipts with fresh canonical before combining original rows.
    # A local same-value recheck may be a real A→B→A change against the cloud.
    # A late or unconfirmed receipt remains local for explicit source recovery.
    transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
    indicator_version="$(remote_version indicators.sqlite)"
    download_indicator_snapshot "${transfer_staging}/indicators.sqlite" "${indicator_version}"
    check_sqlite "${transfer_staging}/indicators.sqlite"
    merge_economic_store "${transfer_staging}/indicators.sqlite" "$(store_path indicators.sqlite)" "$@"
    cleanup_staging
    transfer_staging=""
    push_key_if_version indicators.sqlite "${indicator_version}"
    ;;
  push-app)
    app_version="$(pulled_version baibai.sqlite)"
    push_key_if_version baibai.sqlite "${app_version}"
    ;;
  upload-portfolio-views)
    [[ "${GITHUB_ACTIONS:-}" == "true" ]] || {
      printf 'refusing serving upload outside GitHub Actions\n' >&2
      exit 2
    }
    verify_publication_lease
    upload_portfolio_views "$2"
    ;;
  *)
    usage
    exit 2
    ;;
esac
