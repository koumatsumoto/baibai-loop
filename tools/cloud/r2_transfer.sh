#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
stores_bucket="${R2_STORES_BUCKET:-baibai-stores}"
serving_bucket="${R2_SERVING_BUCKET:-baibai-serving}"
copy_read_timeout=300
transport_compression_level=1
transfer_staging=""
publish_guard_key=""
publish_guard_raw_head=""
publish_guard_compressed_head=""

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

r2_error_reason() {
  case "$1" in
    *PreconditionFailed* | *"(412)"*) printf 'precondition_failed\n' ;;
    *AccessDenied* | *InvalidAccessKeyId* | *SignatureDoesNotMatch* | *ExpiredToken* | *"(403)"*)
      printf 'access_denied\n'
      ;;
    *[Tt]imeout* | *"Could not connect"* | *"endpoint URL"* | *"Connection was closed"*)
      printf 'transport_error\n'
      ;;
    *) printf 'api_error\n' ;;
  esac
}

aws_s3() {
  local operation="${1:-unknown}"
  local error status reason remote="unknown"
  local argument
  for argument in "$@"; do
    if [[ "${argument}" == s3://* ]]; then
      remote="${argument#s3://}"
      break
    fi
  done
  if error="$(aws s3 "$@" \
    --endpoint-url "${endpoint}" --only-show-errors --no-progress \
    2>&1 >/dev/null)"; then
    return 0
  else
    status=$?
  fi
  reason="$(r2_error_reason "${error}")"
  printf 'R2 s3 %s failed for %s (reason=%s, exit=%s)\n' \
    "${operation}" "${remote}" "${reason}" "${status}" >&2
  return "${status}"
}

r2_s3api_mutation() {
  local operation="$1"
  local key="$2"
  shift 2
  local error status reason
  if error="$(aws s3api "${operation}" "$@" --endpoint-url "${endpoint}" \
    2>&1 >/dev/null)"; then
    return 0
  else
    status=$?
  fi
  reason="$(r2_error_reason "${error}")"
  printf 'R2 %s failed for key %s (reason=%s, exit=%s)\n' \
    "${operation}" "${key}" "${reason}" "${status}" >&2
  return "${status}"
}

sqlite_transport() {
  UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
    uv run python "${repo_root}/tools/cloud/sqlite_transport.py" "$@"
}

remote_object_exists() {
  # Exact-key existence, so a sibling object (a `.bak`) never reads as the key
  # itself the way a prefix listing would. `aws s3 ls` is not usable here: it
  # rejects the transfer flags `aws_s3` passes and would fail for every key.
  local error status
  if error="$(aws s3api head-object \
    --bucket "${stores_bucket}" \
    --key "$1" \
    --endpoint-url "${endpoint}" \
    2>&1 >/dev/null)"; then
    return 0
  else
    status=$?
  fi
  if [[ "${error}" =~ \(404\)|NoSuchKey|NotFound|Not\ Found ]]; then
    return 1
  fi
  local reason
  reason="$(r2_error_reason "${error}")"
  printf 'R2 head-object failed for key %s (reason=%s, exit=%s)\n' \
    "$1" "${reason}" "${status}" >&2
  return "${status}"
}

head_remote_key() {
  local key="$1"
  local output="$2"
  local error="${output}.stderr"
  local status
  if aws s3api head-object \
    --bucket "${stores_bucket}" \
    --key "${key}" \
    --endpoint-url "${endpoint}" \
    >"${output}" 2>"${error}"; then
    rm -f -- "${error}"
    return 0
  else
    status=$?
  fi
  if grep -Eq '\(404\)|NoSuchKey|NotFound|Not Found' "${error}"; then
    rm -f -- "${output}" "${error}"
    return 1
  fi
  local error_text reason
  error_text="$(<"${error}")"
  reason="$(r2_error_reason "${error_text}")"
  printf 'R2 head-object failed for key %s (reason=%s, exit=%s)\n' \
    "${key}" "${reason}" "${status}" >&2
  rm -f -- "${output}" "${error}"
  return "${status}"
}

assert_remote_head_unchanged() {
  local key="$1"
  local before="$2"
  local after="${before}.after"
  local status
  if head_remote_key "${key}" "${after}"; then
    :
  else
    status=$?
    printf 'remote object disappeared during transfer: s3://%s/%s\n' \
      "${stores_bucket}" "${key}" >&2
    return "${status}"
  fi
  sqlite_transport compare-heads --left "${before}" --right "${after}"
}

assert_remote_absent() {
  local key="$1"
  local status
  if remote_object_exists "${key}"; then
    printf 'remote object appeared during transfer: s3://%s/%s\n' \
      "${stores_bucket}" "${key}" >&2
    return 2
  else
    status=$?
  fi
  [[ "${status}" -eq 1 ]] || return "${status}"
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

merge_indicator_store() {
  UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
    uv run python "${repo_root}/tools/cloud/merge_indicator_store.py" \
      --source "$1" --target "$2"
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
  copy_remote_key "$1" "$1.bak" "$2"
}

copy_remote_key() {
  local source_key="$1"
  local destination_key="$2"
  local expected_head="$3"
  local expected_etag
  expected_etag="$(sqlite_transport head-etag --head "${expected_head}")"
  r2_s3api_mutation copy-object "${destination_key}" \
    --bucket "${stores_bucket}" \
    --key "${destination_key}" \
    --copy-source "${stores_bucket}/${source_key}" \
    --copy-source-if-match "${expected_etag}" \
    --cli-read-timeout "${copy_read_timeout}"
}

conditional_upload_compressed() {
  local source="$1"
  local key="$2"
  local metadata="$3"
  local expected_head="${4:-}"
  local condition_args
  if [[ -n "${expected_head}" ]]; then
    local expected_etag
    expected_etag="$(sqlite_transport head-etag --head "${expected_head}")"
    condition_args=(--if-match "${expected_etag}")
  else
    condition_args=(--if-none-match '*')
  fi
  r2_s3api_mutation put-object "${key}" \
    --bucket "${stores_bucket}" \
    --key "${key}" \
    --body "${source}" \
    --content-type application/zstd \
    --metadata "${metadata}" \
    "${condition_args[@]}"
}

assert_publish_guard() {
  local key="$1"
  local raw_exists="$2"
  local raw_head="$3"
  local compressed_exists="$4"
  local compressed_head="$5"
  local guard_raw="${publish_guard_raw_head}"
  local guard_compressed="${publish_guard_compressed_head}"
  if [[ "${publish_guard_key}" != "${key}" ]]; then
    local guard_dir="${R2_PUBLISH_GUARD_DIR:-}"
    [[ -n "${guard_dir}" ]] || return 0
    if [[ ! -f "${guard_dir}/${key}.guard" ]]; then
      printf 'publish guard is missing for %s\n' "${key}" >&2
      return 2
    fi
    guard_raw=""
    guard_compressed=""
    [[ -f "${guard_dir}/${key}.raw.head" ]] && \
      guard_raw="$(<"${guard_dir}/${key}.raw.head")"
    [[ -f "${guard_dir}/${key}.zst.head" ]] && \
      guard_compressed="$(<"${guard_dir}/${key}.zst.head")"
  fi
  local expected
  if [[ -n "${guard_raw}" ]]; then
    if [[ "${raw_exists}" != true ]]; then
      printf 'guarded raw object disappeared before publish: %s\n' "${key}" >&2
      return 2
    fi
    expected="${raw_head}.guard"
    printf '%s\n' "${guard_raw}" >"${expected}"
    sqlite_transport compare-heads --left "${expected}" --right "${raw_head}"
  elif [[ "${raw_exists}" == true ]]; then
    printf 'raw object appeared after guarded download: %s\n' "${key}" >&2
    return 2
  fi
  if [[ -n "${guard_compressed}" ]]; then
    if [[ "${compressed_exists}" != true ]]; then
      printf 'guarded compressed object disappeared before publish: %s\n' "${key}" >&2
      return 2
    fi
    expected="${compressed_head}.guard"
    printf '%s\n' "${guard_compressed}" >"${expected}"
    sqlite_transport compare-heads --left "${expected}" --right "${compressed_head}"
  elif [[ "${compressed_exists}" == true ]]; then
    printf 'compressed object appeared after guarded download: %s\n' "${key}" >&2
    return 2
  fi
}

save_publish_guards() {
  local guard_dir="${R2_PUBLISH_GUARD_DIR:-}"
  [[ -n "${guard_dir}" ]] || return 0
  if [[ "${guard_dir}" == / ]]; then
    printf 'refusing unsafe R2_PUBLISH_GUARD_DIR: %s\n' "${guard_dir}" >&2
    return 2
  fi
  mkdir -p "${guard_dir}"
  local key representation source destination
  for key in "$@"; do
    for representation in raw zst; do
      source="${transfer_staging}/${key}.${representation}.head"
      destination="${guard_dir}/${key}.${representation}.head"
      rm -f -- "${destination}"
      [[ -f "${source}" ]] && cp "${source}" "${destination}"
    done
    printf 'sqlite-transport-guard-v1\n' >"${guard_dir}/${key}.guard"
  done
}

stage_remote_store() {
  local key="$1"
  local output="$2"
  local compressed_key="${key}.zst"
  local raw_head="${output}.raw.head"
  local compressed_head="${output}.zst.head"
  local raw_exists=false
  local compressed_exists=false
  local status
  if head_remote_key "${key}" "${raw_head}"; then
    raw_exists=true
  else
    status=$?
    [[ "${status}" -eq 1 ]] || return "${status}"
  fi
  if head_remote_key "${compressed_key}" "${compressed_head}"; then
    compressed_exists=true
  else
    status=$?
    [[ "${status}" -eq 1 ]] || return "${status}"
  fi

  local resolve_args=(resolve)
  [[ "${raw_exists}" == true ]] && resolve_args+=(--raw-head "${raw_head}")
  [[ "${compressed_exists}" == true ]] && \
    resolve_args+=(--compressed-head "${compressed_head}")
  local resolution selection expected_sha expected_size revision
  resolution="$(sqlite_transport "${resolve_args[@]}")"
  read -r selection expected_sha expected_size revision <<<"${resolution}"

  case "${selection}" in
    raw)
      aws_s3 cp "s3://${stores_bucket}/${key}" "${output}"
      check_sqlite "${output}"
      ;;
    compressed|same)
      local downloaded="${output}.download.zst"
      aws_s3 cp "s3://${stores_bucket}/${compressed_key}" "${downloaded}"
      sqlite_transport decompress \
        --source "${downloaded}" \
        --output "${output}" \
        --sha256 "${expected_sha}" \
        --size "${expected_size}"
      check_sqlite "${output}"
      if [[ "${selection}" == same ]]; then
        local raw_download="${output}.raw-download"
        local raw_identity raw_sha raw_size
        aws_s3 cp "s3://${stores_bucket}/${key}" "${raw_download}"
        check_sqlite "${raw_download}"
        raw_identity="$(sqlite_transport digest --path "${raw_download}")"
        read -r raw_sha raw_size <<<"${raw_identity}"
        if [[ "${raw_sha}" != "${expected_sha}" || "${raw_size}" != "${expected_size}" ]]; then
          printf 'raw and compressed objects disagree at migration revision: %s\n' \
            "${key}" >&2
          return 2
        fi
      fi
      ;;
    *)
      printf 'unexpected transport resolution for %s: %s\n' "${key}" "${selection}" >&2
      return 2
      ;;
  esac

  if [[ "${raw_exists}" == true ]]; then
    assert_remote_head_unchanged "${key}" "${raw_head}"
  else
    assert_remote_absent "${key}"
  fi
  if [[ "${compressed_exists}" == true ]]; then
    assert_remote_head_unchanged "${compressed_key}" "${compressed_head}"
  else
    assert_remote_absent "${compressed_key}"
  fi
}

pull_keys() {
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  local key target
  # Every store is downloaded, decompressed, identity-checked, and quick-checked
  # before any operational path is replaced.
  for key in "$@"; do
    stage_remote_store "${key}" "${transfer_staging}/${key}"
  done
  save_publish_guards "$@"
  for key in "$@"; do
    target="$(store_path "${key}")"
    mkdir -p "$(dirname "${target}")"
    mv -f "${transfer_staging}/${key}" "${target}"
  done
  cleanup_staging
  transfer_staging=""
}

publish_compressed_store() {
  local key="$1"
  local publish_mode="$2"
  local compressed_key="${key}.zst"
  local snapshot="${transfer_staging}/${key}"
  local compressed="${snapshot}.zst"
  local identity sha size
  identity="$(<"${snapshot}.identity")"
  read -r sha size <<<"${identity}"

  local raw_head="${snapshot}.publish.raw.head"
  local compressed_head="${snapshot}.publish.zst.head"
  local raw_exists=false
  local compressed_exists=false
  local status selection resolution
  if head_remote_key "${key}" "${raw_head}"; then
    raw_exists=true
  else
    status=$?
    [[ "${status}" -eq 1 ]] || return "${status}"
  fi
  if head_remote_key "${compressed_key}" "${compressed_head}"; then
    compressed_exists=true
  else
    status=$?
    [[ "${status}" -eq 1 ]] || return "${status}"
  fi

  if [[ "${publish_mode}" == seed \
    && ( "${raw_exists}" == true || "${compressed_exists}" == true ) ]]; then
    printf 'refusing initial seed: a representation appeared for %s\n' "${key}" >&2
    return 2
  fi
  if [[ "${publish_mode}" != seed ]]; then
    assert_publish_guard \
      "${key}" "${raw_exists}" "${raw_head}" \
      "${compressed_exists}" "${compressed_head}"
  fi

  if [[ "${compressed_exists}" == true ]]; then
    local resolve_args=(resolve --compressed-head "${compressed_head}")
    [[ "${raw_exists}" == true ]] && resolve_args+=(--raw-head "${raw_head}")
    resolution="$(sqlite_transport "${resolve_args[@]}")"
    local remote_sha remote_size
    read -r selection remote_sha remote_size _ <<<"${resolution}"
    if [[ "${remote_sha}" == "${sha}" && "${remote_size}" == "${size}" ]]; then
      # A retry after an ambiguous response must not rotate the known-good backup.
      # Re-download the current body before accepting the intended snapshot as committed.
      stage_remote_store "${key}" "${snapshot}.already-published"
      return 0
    fi
    if [[ "${selection}" == same ]]; then
      stage_remote_store "${key}" "${snapshot}.baseline-verified"
    fi
  elif [[ "${raw_exists}" == true ]]; then
    # The first compressed generation is the exact retained raw object. It gives
    # readers a verifiable compatibility baseline while the raw key remains intact.
    local baseline="${snapshot}.baseline"
    local baseline_compressed="${baseline}.zst"
    local baseline_identity baseline_sha baseline_size baseline_metadata
    stage_remote_store "${key}" "${baseline}"
    baseline_identity="$(sqlite_transport compress \
      --source "${baseline}" \
      --output "${baseline_compressed}" \
      --level "${transport_compression_level}")"
    read -r baseline_sha baseline_size <<<"${baseline_identity}"
    # Bind the exact HeadObject that stage_remote_store verified around the raw
    # download. A later head must never be paired with the earlier downloaded bytes.
    raw_head="${baseline}.raw.head"
    assert_remote_head_unchanged "${key}" "${raw_head}"
    assert_remote_absent "${compressed_key}"
    baseline_metadata="$(sqlite_transport metadata \
      --mode baseline \
      --sha256 "${baseline_sha}" \
      --size "${baseline_size}" \
      --raw-head "${raw_head}")"
    conditional_upload_compressed \
      "${baseline_compressed}" "${compressed_key}" "${baseline_metadata}"
    assert_remote_head_unchanged "${key}" "${raw_head}"
    head_remote_key "${compressed_key}" "${compressed_head}"
    compressed_exists=true
  fi

  local metadata_args=(
    metadata --mode publish --sha256 "${sha}" --size "${size}"
  )
  [[ "${raw_exists}" == true ]] && metadata_args+=(--raw-head "${raw_head}")
  [[ "${compressed_exists}" == true ]] && \
    metadata_args+=(--previous-head "${compressed_head}")
  local metadata
  metadata="$(sqlite_transport "${metadata_args[@]}")"

  if [[ "${compressed_exists}" == true ]]; then
    backup_remote_key "${compressed_key}" "${compressed_head}"
    assert_remote_head_unchanged "${compressed_key}" "${compressed_head}"
  else
    assert_remote_absent "${compressed_key}"
  fi
  if [[ "${raw_exists}" == true ]]; then
    assert_remote_head_unchanged "${key}" "${raw_head}"
  else
    assert_remote_absent "${key}"
  fi
  local upload_expected_head=""
  [[ "${compressed_exists}" == true ]] && upload_expected_head="${compressed_head}"
  conditional_upload_compressed \
    "${compressed}" "${compressed_key}" "${metadata}" \
    "${upload_expected_head}"

  local published_head="${compressed_head}.published"
  head_remote_key "${compressed_key}" "${published_head}"
  local verify_args=(resolve --compressed-head "${published_head}")
  if [[ "${raw_exists}" == true ]]; then
    assert_remote_head_unchanged "${key}" "${raw_head}"
    verify_args+=(--raw-head "${raw_head}.after")
  else
    assert_remote_absent "${key}"
  fi
  local published_resolution published_selection published_sha published_size
  published_resolution="$(sqlite_transport "${verify_args[@]}")"
  read -r published_selection published_sha published_size _ <<<"${published_resolution}"
  if [[ "${published_selection}" != compressed \
    || "${published_sha}" != "${sha}" \
    || "${published_size}" != "${size}" ]]; then
    printf 'published compressed identity differs from local snapshot: %s\n' "${key}" >&2
    return 2
  fi
}

push_keys() {
  local publish_mode="$1"
  shift
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  local key source
  # Snapshot integrity and compressed round-trip validation for every requested
  # store complete before the first remote backup or upload begins.
  for key in "$@"; do
    source="$(store_path "${key}")"
    snapshot_sqlite "${source}" "${transfer_staging}/${key}"
    sqlite_transport compress \
      --source "${transfer_staging}/${key}" \
      --output "${transfer_staging}/${key}.zst" \
      --level "${transport_compression_level}" \
      >"${transfer_staging}/${key}.identity"
  done
  for key in "$@"; do
    publish_compressed_store "${key}" "${publish_mode}"
  done
  cleanup_staging
  transfer_staging=""
}

seed_keys() {
  local key representation status
  for key in "$@"; do
    for representation in "${key}" "${key}.zst"; do
      if remote_object_exists "${representation}"; then
        printf 'refusing initial seed: s3://%s/%s already exists\n' \
          "${stores_bucket}" "${representation}" >&2
        return 2
      else
        status=$?
      fi
      [[ "${status}" -eq 1 ]] || return "${status}"
    done
  done
  push_keys seed "$@"
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

rollback_market_v13() {
  local daily_confirmation="$1"
  local backfill_confirmation="$2"
  if [[ "${daily_confirmation}" != "--confirm-cloud-daily-batch-disabled" \
    || "${backfill_confirmation}" != "--confirm-cloud-history-backfill-disabled" ]]; then
    printf 'rollback requires explicit confirmation that both market writers are disabled\n' >&2
    return 2
  fi
  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    printf 'refusing market rollback inside GitHub Actions\n' >&2
    return 2
  fi

  local artifact_key="schema-migrations/market-v13.sqlite"
  local compressed_key="market.sqlite.zst"
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  chmod 700 "${transfer_staging}"
  local artifact="${transfer_staging}/market-v13.sqlite"
  local artifact_head="${transfer_staging}/market-v13.head"
  local current="${transfer_staging}/market-before-rollback.sqlite"

  # Nothing remote changes until the immutable artifact, current transport, and
  # content-addressed recovery copies have all been downloaded and validated.
  head_remote_key "${artifact_key}" "${artifact_head}"
  aws_s3 cp "s3://${stores_bucket}/${artifact_key}" "${artifact}"
  [[ -s "${artifact}" ]] || {
    printf 'rollback artifact is empty: %s\n' "${artifact_key}" >&2
    return 2
  }
  check_sqlite_schema "${artifact}" 13
  assert_remote_head_unchanged "${artifact_key}" "${artifact_head}"
  stage_remote_store market.sqlite "${current}"
  local compressed_head="${current}.zst.head"
  if [[ ! -f "${compressed_head}" ]]; then
    printf 'market v14 rollback requires the compressed transport object\n' >&2
    return 2
  fi
  if cmp -s "${artifact}" "${current}"; then
    check_sqlite_schema "${current}" 13
    printf 'market v13 rollback is already published\n'
    cleanup_staging
    transfer_staging=""
    return 0
  fi
  check_sqlite_schema "${current}" 14

  local current_identity current_sha current_size compressed_etag
  current_identity="$(sqlite_transport identity --compressed-head "${compressed_head}")"
  read -r current_sha current_size _ <<<"${current_identity}"
  compressed_etag="$(sqlite_transport head-etag --head "${compressed_head}")"
  local recovery_prefix="schema-rollbacks/market-v13/${compressed_etag}"
  local recovery_compressed_key="${recovery_prefix}/pre-rollback-market.sqlite.zst"
  copy_remote_key "${compressed_key}" "${recovery_compressed_key}" "${compressed_head}"
  assert_remote_head_unchanged "${compressed_key}" "${compressed_head}"

  local recovery_compressed_head="${transfer_staging}/recovery.zst.head"
  local recovery_compressed_etag recovery_identity
  head_remote_key "${recovery_compressed_key}" "${recovery_compressed_head}"
  recovery_compressed_etag="$(sqlite_transport head-etag --head "${recovery_compressed_head}")"
  if [[ "${recovery_compressed_etag}" != "${compressed_etag}" ]]; then
    printf 'compressed recovery copy has a different ETag\n' >&2
    return 2
  fi
  recovery_identity="$(sqlite_transport identity --compressed-head "${recovery_compressed_head}")"
  if [[ "${recovery_identity}" != "${current_identity}" ]]; then
    printf 'compressed recovery copy has different transport metadata\n' >&2
    return 2
  fi
  local recovery_compressed="${transfer_staging}/recovery.zst"
  local recovery_sqlite="${transfer_staging}/recovery.sqlite"
  aws_s3 cp "s3://${stores_bucket}/${recovery_compressed_key}" "${recovery_compressed}"
  sqlite_transport decompress \
    --source "${recovery_compressed}" \
    --output "${recovery_sqlite}" \
    --sha256 "${current_sha}" \
    --size "${current_size}"
  check_sqlite_schema "${recovery_sqlite}" 14

  local raw_head="${current}.raw.head"
  local raw_before="${transfer_staging}/raw-before.sqlite"
  if [[ -f "${raw_head}" ]]; then
    local raw_etag recovery_raw_key recovery_raw
    raw_etag="$(sqlite_transport head-etag --head "${raw_head}")"
    recovery_raw_key="${recovery_prefix}/pre-rollback-market-${raw_etag}.sqlite"
    aws_s3 cp "s3://${stores_bucket}/market.sqlite" "${raw_before}"
    check_sqlite "${raw_before}"
    assert_remote_head_unchanged market.sqlite "${raw_head}"
    copy_remote_key market.sqlite "${recovery_raw_key}" "${raw_head}"
    assert_remote_head_unchanged market.sqlite "${raw_head}"
    recovery_raw="${transfer_staging}/recovery-raw.sqlite"
    aws_s3 cp "s3://${stores_bucket}/${recovery_raw_key}" "${recovery_raw}"
    check_sqlite "${recovery_raw}"
    cmp -s "${raw_before}" "${recovery_raw}" || {
      printf 'raw recovery copy differs from its source\n' >&2
      return 2
    }
  fi

  local rollback_compressed="${transfer_staging}/rollback.sqlite.zst"
  local rollback_identity rollback_sha rollback_size rollback_metadata
  rollback_identity="$(sqlite_transport compress \
    --source "${artifact}" \
    --output "${rollback_compressed}" \
    --level "${transport_compression_level}")"
  read -r rollback_sha rollback_size <<<"${rollback_identity}"
  local rollback_metadata_args=(
    metadata --mode publish
    --sha256 "${rollback_sha}"
    --size "${rollback_size}"
    --previous-head "${compressed_head}"
  )
  [[ -f "${raw_head}" ]] && rollback_metadata_args+=(--raw-head "${raw_head}")
  rollback_metadata="$(sqlite_transport "${rollback_metadata_args[@]}")"
  assert_remote_head_unchanged "${artifact_key}" "${artifact_head}"
  [[ -f "${raw_head}" ]] && assert_remote_head_unchanged market.sqlite "${raw_head}"
  assert_remote_head_unchanged "${compressed_key}" "${compressed_head}"
  conditional_upload_compressed \
    "${rollback_compressed}" "${compressed_key}" "${rollback_metadata}" "${compressed_head}"
  [[ -f "${raw_head}" ]] && assert_remote_head_unchanged market.sqlite "${raw_head}"

  local verified="${transfer_staging}/market-v13-verified.sqlite"
  stage_remote_store market.sqlite "${verified}"
  check_sqlite_schema "${verified}" 13
  printf 'market v13 rollback published; recovery prefix: s3://%s/%s/\n' \
    "${stores_bucket}" "${recovery_prefix}"
  cleanup_staging
  transfer_staging=""
}

upload_serving() {
  local output_dir="$1"
  local concurrency="${R2_SERVING_UPLOAD_CONCURRENCY:-10}"
  local aws_config
  if [[ ! -f "${output_dir}/views/meta.json" ]]; then
    printf 'serving export is incomplete: %s/views/meta.json is missing\n' "${output_dir}" >&2
    return 2
  fi
  case "${concurrency}" in
    10 | 20 | 40) ;;
    *)
      printf 'R2_SERVING_UPLOAD_CONCURRENCY must be one of 10, 20, or 40; got %s\n' \
        "${concurrency}" >&2
      return 2
      ;;
  esac

  # Keep the default at the AWS CLI baseline until the isolated benchmark selects
  # an arm. A repository variable can then adopt or roll back the measured value
  # without changing the upload contract or requiring another code change.
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  aws_config="${transfer_staging}/aws-config"
  printf '[default]\nregion = auto\ns3 =\n    max_concurrent_requests = %s\n' \
    "${concurrency}" >"${aws_config}"

  AWS_CONFIG_FILE="${aws_config}" aws_s3 \
    sync "${output_dir}/views/" "s3://${serving_bucket}/views/" \
    --delete --exclude meta.json
  if [[ -d "${output_dir}/history/candidate-views" ]]; then
    AWS_CONFIG_FILE="${aws_config}" aws_s3 \
      sync "${output_dir}/history/candidate-views/" \
      "s3://${serving_bucket}/history/candidate-views/"
  fi
  # Freshness is published only after every view and history upload succeeds.
  AWS_CONFIG_FILE="${aws_config}" aws_s3 \
    cp "${output_dir}/views/meta.json" "s3://${serving_bucket}/views/meta.json"
  cleanup_staging
  transfer_staging=""
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
  printf 'usage: %s {pull-all|pull-machine|pull-market|preserve-market-v13|download-market-v13-rollback FILE|rollback-market-v13 --confirm-cloud-daily-batch-disabled --confirm-cloud-history-backfill-disabled|seed-all|push-machine|push-market|push-macro|push-app|upload-serving DIR|upload-run-summary FILE}\n' "$0" >&2
}

load_credentials
case "${1:-}" in
  pull-all)
    pull_keys market.sqlite runs.sqlite macro.sqlite baibai.sqlite
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
  preserve-market-v13)
    preserve_market_v13
    ;;
  download-market-v13-rollback)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    download_market_v13_rollback "$2"
    ;;
  rollback-market-v13)
    [[ $# -eq 3 ]] || { usage; exit 2; }
    rollback_market_v13 "$2" "$3"
    ;;
  seed-all)
    seed_keys market.sqlite runs.sqlite macro.sqlite baibai.sqlite
    ;;
  push-machine)
    if [[ "${GITHUB_ACTIONS:-}" != "true" ]]; then
      printf 'refusing machine-store push outside GitHub Actions\n' >&2
      exit 2
    fi
    push_keys update market.sqlite runs.sqlite macro.sqlite
    ;;
  push-market)
    if [[ "${GITHUB_ACTIONS:-}" != "true" ]]; then
      printf 'refusing machine-store push outside GitHub Actions\n' >&2
      exit 2
    fi
    push_keys update market.sqlite
    ;;
  push-macro)
    # Deep history is fetched locally with `macro refresh --all-history`, which the
    # cloud's rolling-window refresh never reaches, while the daily batch keeps adding
    # recent observations the local store has never seen. So the local store is only
    # publishable once it contains the cloud copy: pull it, merge it in, and let the
    # merge refuse the upload if any cloud row would be left behind.
    transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
    stage_remote_store macro.sqlite "${transfer_staging}/macro.sqlite"
    publish_guard_key=macro.sqlite
    [[ -f "${transfer_staging}/macro.sqlite.raw.head" ]] && \
      publish_guard_raw_head="$(<"${transfer_staging}/macro.sqlite.raw.head")"
    [[ -f "${transfer_staging}/macro.sqlite.zst.head" ]] && \
      publish_guard_compressed_head="$(<"${transfer_staging}/macro.sqlite.zst.head")"
    merge_indicator_store "${transfer_staging}/macro.sqlite" "$(store_path macro.sqlite)"
    cleanup_staging
    transfer_staging=""
    push_keys update macro.sqlite
    ;;
  push-app)
    push_keys update baibai.sqlite
    ;;
  upload-serving)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    upload_serving "$2"
    ;;
  upload-run-summary)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    upload_run_summary "$2"
    ;;
  *)
    usage
    exit 2
    ;;
esac
