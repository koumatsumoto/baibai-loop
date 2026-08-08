#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
(cd "${repo_root}" && uv run python -m baibai_batch.validation.repository_layout)
stores_bucket="${R2_STORES_BUCKET:-baibai-stores}"
serving_bucket="${R2_SERVING_BUCKET:-baibai-serving}"
copy_read_timeout=300
transfer_staging=""
transfer_config=""
# The serving prefix is thousands of small objects, so its wall clock is request
# latency and not bytes: 54MB across 3,740 objects took 282s, which is 13 requests
# a second against the CLI's default of 10 in flight. The in-flight count is
# therefore the only lever on that number.
serving_upload_concurrency_default=32

cleanup_staging() {
  if [[ -n "${transfer_staging}" && -d "${transfer_staging}" ]]; then
    case "${transfer_staging}" in
      "${repo_root}"/.r2-transfer.*) rm -r -- "${transfer_staging}" ;;
      *) printf 'refusing to remove unexpected staging path: %s\n' "${transfer_staging}" >&2 ;;
    esac
  fi
  if [[ -n "${transfer_config}" && -f "${transfer_config}" ]]; then
    rm -f -- "${transfer_config}"
    transfer_config=""
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

use_serving_transfer_settings() {
  # `max_concurrent_requests` has no command-line flag, and `aws configure set`
  # would write it into the caller's own `~/.aws/config`. This script also runs on
  # a developer machine, so the setting is confined to a throwaway config this
  # process points at. Credentials keep coming from the environment, which takes
  # precedence, so the file carries transfer settings and nothing else. Two of
  # these can run at once, so each gets its own file.
  #
  # The value is read here rather than at load time so `.env` (sourced by
  # `load_credentials`, which runs first) can set it as well as the step env.
  local concurrency
  concurrency="${R2_SERVING_UPLOAD_CONCURRENCY:-${serving_upload_concurrency_default}}"
  transfer_config="$(mktemp "${TMPDIR:-/tmp}/baibai-r2-transfer.XXXXXX")"
  printf '[default]\ns3 =\n  max_concurrent_requests = %s\n' \
    "${concurrency}" > "${transfer_config}"
  export AWS_CONFIG_FILE="${transfer_config}"
  # The version and the value the CLI actually resolves are printed because the
  # setting has no flag to echo back: a runner whose CLI reads the config
  # differently would otherwise transfer at the default and look identical.
  printf 'serving transfer: max_concurrent_requests=%s aws=%s resolved=%s\n' \
    "${concurrency}" \
    "$(aws --version 2>&1)" \
    "$(aws configure get s3.max_concurrent_requests 2>/dev/null || printf 'unset')"
}

# How many objects a stage is about to move. Reported next to the elapsed seconds
# so a run's log answers whether raising the in-flight count changed anything.
count_files() {
  local directory="$1"
  if [[ ! -d "${directory}" ]]; then
    printf '0\n'
    return 0
  fi
  find "${directory}" -type f | wc -l | tr -d ' \n'
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
    market.sqlite) printf '%s/stores/market/market.sqlite\n' "${repo_root}" ;;
    runs.sqlite) printf '%s/stores/screening/runs.sqlite\n' "${repo_root}" ;;
    macro.sqlite) printf '%s/stores/macro/macro.sqlite\n' "${repo_root}" ;;
    baibai.sqlite) printf '%s/stores/application/baibai.sqlite\n' "${repo_root}" ;;
    *) printf 'unknown store key: %s\n' "$1" >&2; return 2 ;;
  esac
}

check_sqlite() {
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m baibai_batch.storage.sqlite_snapshot check --path "$1"
  )
}

check_sqlite_schema() {
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m baibai_batch.storage.sqlite_snapshot check \
        --path "$1" --schema-version "$2"
  )
}

sqlite_schema_version() {
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m baibai_batch.storage.sqlite_snapshot version --path "$1"
  )
}

snapshot_sqlite() {
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m baibai_batch.storage.sqlite_snapshot create \
        --source "$1" --output "$2"
  )
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
  merge_store baibai_batch.storage.merge_indicator_store "$1" "$2"
}

# Move the downloaded copy to the schema this code writes, so the merge's source and
# target agree. Without it a store published before a migration landed can only be
# moved forward by the daily batch, and every schema change would block publishing
# from a developer machine until the cloud had run. The copy lives in the transfer
# staging directory, so the object in R2 is untouched.
migrate_downloaded_store() {
  local store="$1" path="$2"
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m baibai_batch.storage.migrate_store --store "${store}" --path "${path}"
  )
}

merge_market_store() {
  merge_store baibai_batch.storage.merge_market_store "$1" "$2"
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

pull_keys() {
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  local key target index
  # The stores download one after another, so a daily batch that pushes partway
  # through leaves a local set whose members come from either side of that push:
  # a run store that knows a screening run the market store has no bars for.
  # `check_sqlite` reads each store on its own and passes such a set, and nothing
  # downstream re-derives the combination, so screening would read a cross-section
  # that never existed. Comparing each object's version before and after the
  # downloads catches exactly that straddle, and the local stores stay untouched.
  local -a versions=()
  for key in "$@"; do
    versions+=("$(remote_version "${key}")")
  done
  for key in "$@"; do
    aws_s3 cp "s3://${stores_bucket}/${key}" "${transfer_staging}/${key}"
    check_sqlite "${transfer_staging}/${key}"
  done
  index=0
  for key in "$@"; do
    if [[ "$(remote_version "${key}")" != "${versions[index]}" ]]; then
      printf 'refusing to replace local stores: %s changed on R2 during the pull. ' "${key}" >&2
      printf 'Wait for the daily batch to finish and pull again.\n' >&2
      exit 1
    fi
    index=$((index + 1))
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
  # A push is three waits, and only one of them moves bytes over the link: the local
  # full copy the snapshot makes, the server-side copy that keeps a generation, and
  # the upload. Sending fewer bytes — compressed, or as a diff — shortens the third
  # and leaves the other two exactly as they are, so the split is what says whether
  # either is worth its machinery. The stores differ by a factor of 30 in size, and
  # the two server-side phases scale with it, so the split is reported per key.
  local key source started
  local -a snapshot_seconds=()
  for key in "$@"; do
    source="$(store_path "${key}")"
    started="${SECONDS}"
    snapshot_sqlite "${source}" "${transfer_staging}/${key}"
    snapshot_seconds+=("$((SECONDS - started))")
  done
  local index=0
  for key in "$@"; do
    started="${SECONDS}"
    backup_remote_key "${key}"
    local backup_elapsed=$((SECONDS - started))
    started="${SECONDS}"
    aws_s3 cp "${transfer_staging}/${key}" "s3://${stores_bucket}/${key}"
    printf 'store push: key=%s bytes=%s snapshot=%ss backup=%ss upload=%ss\n' \
      "${key}" \
      "$(wc -c < "${transfer_staging}/${key}" | tr -d ' \n')" \
      "${snapshot_seconds[index]}" \
      "${backup_elapsed}" \
      "$((SECONDS - started))"
    index=$((index + 1))
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

require_complete_export() {
  local output_dir="$1"
  if [[ ! -f "${output_dir}/views/meta.json" ]]; then
    printf 'serving export is incomplete: %s/views/meta.json is missing\n' "${output_dir}" >&2
    return 2
  fi
}

upload_serving_views() {
  # The mutable image of the current run: every view except the freshness claim,
  # which is nearly all of what a run publishes. All of it is rewritten each
  # business day, so this is the part worth overlapping with the store push and
  # the part a failed run can leave behind without lasting harm.
  local output_dir="$1"
  require_complete_export "${output_dir}"
  use_serving_transfer_settings
  local started objects
  started="${SECONDS}"
  # `meta.json` is excluded from this mirror and published by the tail stage, so it
  # is not one of the objects this stage moves. `require_complete_export` has
  # already established that it is there to subtract.
  objects="$(( $(count_files "${output_dir}/views") - 1 ))"
  aws_s3 sync "${output_dir}/views/" "s3://${serving_bucket}/views/" \
    --delete --exclude meta.json
  printf 'serving views: objects=%s elapsed=%ss\n' "${objects}" "$((SECONDS - started))"
}

publish_serving_tail() {
  # The parts that outlive the run: `history/` is append-only and never deleted,
  # and `meta.json` is the freshness claim the UI reads. Both are published only
  # once the store holding this run has been persisted, so a run whose store push
  # failed leaves no permanent record of a run the store does not contain.
  local output_dir="$1"
  require_complete_export "${output_dir}"
  use_serving_transfer_settings
  local started objects
  started="${SECONDS}"
  objects="$(count_files "${output_dir}/history")"
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
  printf 'serving tail: objects=%s elapsed=%ss\n' "$((objects + 1))" "$((SECONDS - started))"
}

pull_app() {
  # application DB の正本はローカルにある (判断はローカルで publish し、`push-app` で
  # cloud へ出す)。cloud copy で置換してよいのは「ローカルに正本が無い」ときだけなので、
  # ファイルがあれば止める。publish 済みで未 push の判断は再生成できず、上書きすると
  # 復元手段が無い。CI は checkout 直後で stores/application/ が空なので素通りする。
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
  # The workflow run summary lives outside `views/`, which `upload_serving_views`
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
  printf 'usage: %s {pull-machine|pull-app|pull-market|pull-longlist-history DIR|seed-all|push-machine|push-market|push-macro|push-app|upload-serving-views DIR|publish-serving-tail DIR|upload-run-summary FILE}\n' "$0" >&2
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
    migrate_downloaded_store market "${transfer_staging}/market.sqlite"
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
    migrate_downloaded_store macro "${transfer_staging}/macro.sqlite"
    merge_indicator_store "${transfer_staging}/macro.sqlite" "$(store_path macro.sqlite)"
    cleanup_staging
    transfer_staging=""
    push_keys macro.sqlite
    ;;
  push-app)
    push_keys baibai.sqlite
    ;;
  # The serving bucket has one writer: the batch that produces a complete export.
  # The views mirror runs with `--delete`, so a partial local export would remove
  # production views, and the local credential carries write permission because R2
  # grants it per token rather than per bucket. The boundary lives here instead.
  #
  # The publish is two stages so the mutable image can overlap the store push while
  # the durable record cannot: see the two functions for which is which.
  upload-serving-views)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    if [[ "${GITHUB_ACTIONS:-}" != "true" ]]; then
      printf 'refusing serving upload outside GitHub Actions\n' >&2
      exit 2
    fi
    upload_serving_views "$2"
    ;;
  publish-serving-tail)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    if [[ "${GITHUB_ACTIONS:-}" != "true" ]]; then
      printf 'refusing serving upload outside GitHub Actions\n' >&2
      exit 2
    fi
    publish_serving_tail "$2"
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
