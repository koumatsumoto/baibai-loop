#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
(cd "${repo_root}" && uv run python -m baibai_jobs.validation.repository_layout)
stores_bucket="${R2_STORES_BUCKET:-baibai-stores}"
generation_dir="${R2_GENERATION_DIR:-${repo_root}/stores/.r2-generations}"
# The lake mirror is a fetch-through cache of immutable objects, so it is named next to
# the stores rather than inside one: every key it holds begins with `lake/`, which is why
# the mirror root is the directory the stores live in and not one of them.
lake_mirror="${R2_LAKE_MIRROR:-${repo_root}/stores}"
copy_read_timeout=300
# The three machine stores are pushed one after another, each conditional on the
# generation the batch pulled, and three PUTs cannot be made one commit. A push that
# stops partway therefore leaves a set nothing ever wrote: the earlier keys at the new
# generation, the rest at the old one. Per key that state is indistinguishable from a
# healthy one — the pull's straddle check compares each object with itself — so the
# receipt is what records which three generations were last seen together. It is
# rewritten by every push that changes a member and checked before a bundle pull
# replaces the local stores.
# The application store is the one object here nothing regenerates: it holds the
# judgments and the ledger. A single `.bak` is a single undo, and a store damaged before
# anyone looked at it has already spent that undo by the second push, so this key keeps
# one dated backup key per day instead, retaining the latest 14 distinct dates.
# Further pushes on the same date replace that day's backup; this is not an archive
# of every application update.
app_backup_generations=14
machine_manifest_key="machine-manifest.json"
machine_bundle_keys=(market.sqlite runs.sqlite indicators.sqlite)
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
  # Only an explicit missing object means absence. Authentication, network and server
  # failures must stop the script even when this function is called inside an `if`.
  local error
  if error="$(aws s3api head-object \
    --bucket "${stores_bucket}" \
    --key "$1" \
    --endpoint-url "${endpoint}" 2>&1 >/dev/null)"; then
    return 0
  fi
  if [[ "${error}" == *"(404) when calling the HeadObject operation"* ]]; then
    return 1
  fi
  printf 'cannot determine whether R2 object %s exists: %s\n' "$1" "${error}" >&2
  exit 2
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

merge_economic_store() {
  merge_store baibai_jobs.integration.native_source_merge "$1" "$2"
}

merge_market_store() {
  merge_store baibai_jobs.integration.source_cache_merge "$1" "$2"
}

hydrate_market() {
  # The store arrives from R2 holding only what the lake does not own. Filling it is
  # what makes it the store every reader already expects, and it fails closed on the
  # published row counts, so a fill that silently did nothing cannot reach screening.
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run baibai-engine snapshots hydrate \
        --mirror "${lake_mirror}" \
        --store "$(store_path market.sqlite)" \
        --bucket "${stores_bucket}"
  )
}

publish_lake() {
  # Derive every partition from the store, seal a release, and switch the pointer. The
  # publication refuses when the origin embedded in that same SQLite file differs from
  # current, so no detached identity file can authorize stale rows. The JSON report is
  # stdout only; the durable identity lives in market.sqlite.
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m baibai_jobs.publication.snapshot_publication \
        --sqlite "$(store_path market.sqlite)" \
        --mirror "${lake_mirror}" \
        --bucket "${stores_bucket}" \
        --lease-handle "${PUBLICATION_LEASE_HANDLE:?publication lease handle is required}"
  )
}

dehydrate_market_snapshot() {
  # Runs on the upload copy, never on the working store. Origin and row counts must
  # match the release; values are not compared. Publish local changes first and do not
  # edit lake-owned rows between publication and snapshotting.
  local path="$1"
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run baibai-engine snapshots dehydrate \
        --mirror "${lake_mirror}" \
        --store "${path}" \
        --bucket "${stores_bucket}"
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
  # `remote_version` returns the ETag as the API quotes it. The receipt holds the bare
  # value, and the charset it is required to sit in is what keeps the receipt JSON it
  # never has to escape. Anything outside it is refused here rather than written out
  # and compared later.
  local value="$1"
  value="${value%\"}"
  value="${value#\"}"
  if [[ ! "${value}" =~ ^[A-Za-z0-9._:-]+$ ]]; then
    printf 'unusable object version: %s\n' "$1" >&2
    return 1
  fi
  printf '%s\n' "${value}"
}

write_machine_manifest() {
  # The versions are read back rather than carried out of the PUTs. The receipt is a
  # statement about what the bucket holds now, so a write landing after this read leaves
  # it merely stale — which the next bundle pull refuses. Values carried forward from
  # the pushes would instead let it claim a generation nobody ever observed together.
  local key version file first=1
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  file="${transfer_staging}/machine-manifest.json"
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "written_at_utc": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '  "keys": {\n'
    for key in "${machine_bundle_keys[@]}"; do
      version="$(etag_body "$(remote_version "${key}")")"
      if [[ ${first} -eq 0 ]]; then
        printf ',\n'
      fi
      printf '    "%s": "%s"' "${key}" "${version}"
      first=0
    done
    printf '\n  }\n}\n'
  } > "${file}"
  aws s3api put-object \
    --bucket "${stores_bucket}" \
    --key "${machine_manifest_key}" \
    --body "${file}" \
    --endpoint-url "${endpoint}" \
    >/dev/null
  cleanup_staging
  transfer_staging=""
  printf 'machine bundle receipt written: %s\n' "${machine_manifest_key}"
}

manifest_recorded_version() {
  # Read the JSON contract, not the writer's whitespace or line layout.
  python3 - "$1" "$2" <<'PYTHON'
import json
import re
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as source:
        receipt = json.load(source)
except (OSError, ValueError):
    receipt = None
keys = receipt.get("keys") if isinstance(receipt, dict) else None
value = keys.get(sys.argv[2]) if isinstance(keys, dict) else None
print(value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]+", value) else "")
PYTHON
}

require_machine_manifest() {
  # Each argument is `key=version` for the generation this pull is about to take. A
  # mismatch is either a push running right now or one that stopped partway. The two
  # need different answers: the first clears itself when that push finishes, and the
  # second clears nothing on its own — the conditional PUTs are bound to the generation
  # the failed run pulled, so re-running the same command is refused by its own
  # precheck. The message names both rather than telling the reader to retry.
  local file entry key expected actual
  file="${transfer_staging}/machine-manifest.json"
  # Absence is decided by the existence check, not by the download failing: a download
  # that fails for any other reason must refuse the pull rather than read as "no receipt
  # was ever written", which is the shape a hole in this check would take. The existence
  # check distinguishes a missing object from authentication, network or server errors;
  # success for another key does not establish that this request succeeded.
  if ! remote_object_exists "${machine_manifest_key}"; then
    printf 'no machine bundle receipt in s3://%s yet; taking the store set unverified. ' \
      "${stores_bucket}" >&2
    printf 'The next machine push writes one.\n' >&2
    return 0
  fi
  aws s3api get-object \
    --bucket "${stores_bucket}" \
    --key "${machine_manifest_key}" \
    --endpoint-url "${endpoint}" \
    "${file}" >/dev/null
  if [[ ! -s "${file}" ]]; then
    printf 'machine bundle receipt downloaded empty from s3://%s/%s\n' \
      "${stores_bucket}" "${machine_manifest_key}" >&2
    exit 1
  fi
  for entry in "$@"; do
    key="${entry%%=*}"
    actual="$(etag_body "${entry#*=}")"
    expected="$(manifest_recorded_version "${file}" "${key}")"
    if [[ "${expected}" != "${actual}" ]]; then
      printf 'refusing to replace local stores: R2 holds %s at generation %s but the ' \
        "${key}" "${actual}" >&2
      printf 'machine bundle receipt names %s.\n' "${expected:-no generation}" >&2
      printf 'If a machine push is running right now, wait for it to finish and pull ' >&2
      printf 'again. If one stopped partway, no retry clears this by itself: follow ' >&2
      printf '"部分 push からの復旧" in jobs/OPERATIONS.md.\n' >&2
      exit 1
    fi
  done
  printf 'machine bundle receipt matches the store generations being pulled\n'
}

pull_keys() {
  local verify_receipt=0
  if [[ "${1:-}" == "--bundle-receipt" ]]; then
    verify_receipt=1
    shift
  fi
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
  clear_pulled_versions "$@"
  for key in "$@"; do
    versions+=("$(remote_version "${key}")")
  done
  if [[ ${verify_receipt} -eq 1 ]]; then
    local -a observed=()
    index=0
    for key in "$@"; do
      observed+=("${key}=${versions[index]}")
      index=$((index + 1))
    done
    require_machine_manifest "${observed[@]}"
  fi
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
  index=0
  for key in "$@"; do
    record_pulled_version "${key}" "${versions[index]}"
    index=$((index + 1))
  done
  cleanup_staging
  transfer_staging=""
}

backup_key_for() {
  # Dated for the application store and one fixed backup for each machine store.
  # Machine data is derived/acquired, but expired upstream history may not be
  # recoverable; this backup is recovery protection, not proof of rebuildability.
  case "$1" in
    baibai.sqlite) printf '%s.bak-%s\n' "$1" "$(TZ=Asia/Tokyo date +%Y%m%d)" ;;
    *) printf '%s.bak\n' "$1" ;;
  esac
}

prune_app_backups() {
  # Only keys this script writes are considered, and each delete names one exact key.
  # A prefix delete would take whatever else happened to sit under the prefix, and the
  # pattern is what keeps the single pre-dated `baibai.sqlite.bak` out of the set.
  local listing key
  local -a generations=()
  listing="$(aws s3api list-objects-v2 \
    --bucket "${stores_bucket}" \
    --prefix "baibai.sqlite.bak-" \
    --query 'Contents[].Key' \
    --output text \
    --endpoint-url "${endpoint}")"
  for key in ${listing}; do
    if [[ "${key}" == "None" ]]; then
      continue
    fi
    if [[ ! "${key}" =~ ^baibai\.sqlite\.bak-[0-9]{8}$ ]]; then
      printf 'leaving an unrecognised key under the application backup prefix: %s\n' \
        "${key}" >&2
      continue
    fi
    generations+=("${key}")
  done
  local index=0
  while read -r key; do
    [[ -n "${key}" ]] || continue
    index=$((index + 1))
    if [[ ${index} -le ${app_backup_generations} ]]; then
      continue
    fi
    aws s3api delete-object \
      --bucket "${stores_bucket}" \
      --key "${key}" \
      --endpoint-url "${endpoint}" \
      >/dev/null
    printf 'pruned application store backup: %s\n' "${key}"
  done < <(printf '%s\n' ${generations+"${generations[@]}"} | sort -r)
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
  local key="$1" expected_version="${2:-}"
  if remote_object_exists "${key}"; then
    local backup_key
    backup_key="$(backup_key_for "${key}")"
    local -a source_condition=()
    if [[ -n "${expected_version}" ]]; then
      source_condition+=(--copy-source-if-match "${expected_version}")
    fi
    aws s3api copy-object \
      --bucket "${stores_bucket}" \
      --key "${backup_key}" \
      --copy-source "${stores_bucket}/${key}" \
      "${source_condition[@]}" \
      --endpoint-url "${endpoint}" \
      --cli-read-timeout "${copy_read_timeout}" \
      >/dev/null
  fi
}

_push_keys() {
  local expected_version="$1"
  shift
  if [[ -n "${expected_version}" && $# -ne 1 ]]; then
    printf 'conditional store push requires exactly one key\n' >&2
    return 2
  fi
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  # A push is three waits, and only one of them moves bytes over the link: the local
  # full copy the snapshot makes, the server-side copy that keeps a generation, and
  # the upload. Sending fewer bytes — compressed, or as a diff — shortens the third
  # and leaves the other two exactly as they are, so the split is what says whether
  # either is worth its machinery. The stores differ by a factor of 30 in size, and
  # the two server-side phases scale with it, so the split is reported per key.
  local key source started uploaded_version
  local -a snapshot_seconds=()
  for key in "$@"; do
    source="$(store_path "${key}")"
    started="${SECONDS}"
    snapshot_sqlite "${source}" "${transfer_staging}/${key}"
    # The copy that leaves here carries only what the lake does not hold — but only
    # once the lake holds anything. A lake release pointer is where that authority is
    # declared, so it is read rather than assumed: before the first publication (an
    # empty bucket being seeded) the store is still the only copy of those rows, and
    # after it the emptying is mandatory rather than best-effort.
    if [[ "${key}" == "market.sqlite" ]] \
      && remote_object_exists "lake/pointers/market/current.json"; then
      dehydrate_market_snapshot "${transfer_staging}/${key}"
    fi
    snapshot_seconds+=("$((SECONDS - started))")
  done
  local index=0
  for key in "$@"; do
    started="${SECONDS}"
    if [[ -n "${expected_version}" ]]; then
      backup_remote_key "${key}" "${expected_version}"
    fi
    local backup_elapsed=$((SECONDS - started))
    started="${SECONDS}"
    if [[ -n "${expected_version}" ]]; then
      # PutObject's If-Match is checked atomically when R2 commits the object. The
      # earlier read avoids an expensive snapshot after an already-visible race;
      # this condition closes the remaining race through backup and upload.
      uploaded_version="$(aws s3api put-object \
        --bucket "${stores_bucket}" \
        --key "${key}" \
        --body "${transfer_staging}/${key}" \
        --if-match "${expected_version}" \
        --endpoint-url "${endpoint}" \
        --query ETag --output text)"
      if [[ "${key}" == "baibai.sqlite" ]]; then
        # Only this PUT's response identifies our snapshot; a later HEAD could
        # observe a different writer and authorize a stale working copy.
        clear_pulled_versions "${key}"
        if [[ -z "${uploaded_version}" || "${uploaded_version}" == "None" ]]; then
          printf 'application uploaded but no ETag returned; pull before next update\n' >&2
          return 1
        fi
        record_pulled_version "${key}" "${uploaded_version}"
      fi
    else
      # Seed is create-only even if another writer appears after the existence check.
      aws s3api put-object \
        --bucket "${stores_bucket}" \
        --key "${key}" \
        --body "${transfer_staging}/${key}" \
        --if-none-match '*' \
        --endpoint-url "${endpoint}" \
        >/dev/null
    fi
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

push_key_if_version() {
  local key="$1" expected_version="$2" current_version
  current_version="$(remote_version "${key}")"
  if [[ "${current_version}" != "${expected_version}" ]]; then
    printf 'refusing store push: %s changed on R2 during the merge. ' "${key}" >&2
    printf 'Merge the latest cloud copy and try again.\n' >&2
    return 1
  fi
  _push_keys "${expected_version}" "${key}"
}

push_pulled_keys() {
  local key index current_version
  local -a keys=("$@")
  local -a versions=()
  for key in "${keys[@]}"; do
    versions+=("$(pulled_version "${key}")")
  done
  # Check the whole set before any key is written. Each conditional push checks its
  # key again and binds the final PUT, so a later race cannot overwrite that writer.
  index=0
  for key in "${keys[@]}"; do
    current_version="$(remote_version "${key}")"
    if [[ "${current_version}" != "${versions[index]}" ]]; then
      printf 'refusing machine-store push: %s changed on R2 after the pull. ' "${key}" >&2
      printf 'Pull a fresh store set and run the batch again.\n' >&2
      return 1
    fi
    index=$((index + 1))
  done
  index=0
  for key in "${keys[@]}"; do
    push_key_if_version "${key}" "${versions[index]}"
    index=$((index + 1))
  done
  write_machine_manifest
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
  _push_keys "" "$@"
}

pull_app() {
  # application DB の正本はR2にある (判断は取得した作業copyで publish し、`push-app` で
  # cloud へ反映する)。未公開変更を失わないよう、既存の作業copyは置換しない。
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

# The new dashboard has its own serving key: publishing before the UI cutover
# must not remove fields still read by the old deployed dashboard. Old keys stay untouched.
upload_portfolio_views() {
  local directory="$1" name
  for name in portfolio-dashboard tasks outcomes; do
    [[ -s "${directory}/views/${name}.json" ]] || {
      printf 'missing portfolio view: %s\n' "${name}" >&2
      return 2
    }
  done
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  for name in portfolio-dashboard tasks outcomes; do
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
  for name in portfolio-dashboard tasks outcomes; do
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
  printf 'portfolio views verified from remote readback: 3\n'
}

usage() {
  printf 'usage: %s {pull-machine|pull-app|pull-market|pull-runs|pull-indicators|seed-all|hydrate-market|publish-lake|push-machine|push-runs|push-market|push-indicators|push-app|upload-portfolio-views DIR}\n' "$0" >&2
}

load_credentials
case "${1:-}" in
  pull-app)
    pull_app
    ;;
  pull-machine)
    pull_keys --bundle-receipt "${machine_bundle_keys[@]}"
    ;;
  # The market store arrives holding only what the lake does not own; these two are the
  # halves that put the rest in and take it back out. They are separate from the pull and
  # the push because their time and their transfer are worth reporting on their own.
  hydrate-market)
    hydrate_market
    ;;
  publish-lake)
    [[ $# -eq 1 ]] || { usage; exit 2; }
    publish_lake
    ;;
  # A pass that only writes the market store round-trips the other two for nothing,
  # and pushing them back unchanged after hours would revert whatever else wrote them
  # meanwhile. These two move the market store alone.
  pull-market)
    pull_keys market.sqlite
    ;;
  pull-runs)
    pull_keys runs.sqlite
    ;;
  pull-indicators)
    pull_keys indicators.sqlite
    ;;
  seed-all)
    seed_keys market.sqlite runs.sqlite indicators.sqlite baibai.sqlite
    ;;
  push-machine)
    push_pulled_keys "${machine_bundle_keys[@]}"
    ;;
  push-runs)
    # Explicit model-store maintenance; never re-upload unrelated machine stores.
    [[ $# -eq 1 ]] || { usage; exit 2; }
    push_pulled_keys runs.sqlite
    ;;
  push-market)
    # Deep history is fetched where there is time for it — hours of provider calls for a
    # decade of bars — while the daily batch keeps adding recent days the deep copy has
    # never seen. So the local store is only publishable once it contains the cloud copy:
    # pull it, merge it in, and let the merge refuse the upload if any cloud row would be
    # left behind. That check is what makes this safe to run outside GitHub Actions,
    # where an unconditional upload would roll the daily batch back.
    transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
    market_version="$(remote_version market.sqlite)"
    aws_s3 cp "s3://${stores_bucket}/market.sqlite" "${transfer_staging}/market.sqlite"
    check_sqlite "${transfer_staging}/market.sqlite"
    merge_market_store "${transfer_staging}/market.sqlite" "$(store_path market.sqlite)"
    cleanup_staging
    transfer_staging=""
    push_key_if_version market.sqlite "${market_version}"
    write_machine_manifest
    ;;
  push-indicators)
    # Deep history is fetched locally with `collection economic refresh --all-history`, which the
    # cloud's rolling-window refresh never reaches, while the daily batch keeps adding
    # recent observations the local store has never seen. So the local store is only
    # publishable once it contains the cloud copy: pull it, merge it in, and let the
    # merge refuse the upload if any cloud row would be left behind.
    transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
    indicator_version="$(remote_version indicators.sqlite)"
    aws_s3 cp "s3://${stores_bucket}/indicators.sqlite" "${transfer_staging}/indicators.sqlite"
    check_sqlite "${transfer_staging}/indicators.sqlite"
    merge_economic_store "${transfer_staging}/indicators.sqlite" "$(store_path indicators.sqlite)"
    cleanup_staging
    transfer_staging=""
    push_key_if_version indicators.sqlite "${indicator_version}"
    write_machine_manifest
    ;;
  push-app)
    case "${R2_APP_BACKUP_PRUNE:-retain}" in
      retain|skip) ;;
      *) printf 'R2_APP_BACKUP_PRUNE must be retain or skip\n' >&2; exit 2 ;;
    esac
    app_version="$(pulled_version baibai.sqlite)"
    push_key_if_version baibai.sqlite "${app_version}"
    if [[ "${R2_APP_BACKUP_PRUNE:-retain}" == "skip" ]]; then
      printf 'application backup pruning skipped\n'
    else
      prune_app_backups
    fi
    ;;
  upload-portfolio-views)
    [[ $# -eq 2 ]] || { usage; exit 2; }
    [[ "${GITHUB_ACTIONS:-}" == "true" ]] || {
      printf 'refusing serving upload outside GitHub Actions\n' >&2
      exit 2
    }
    upload_portfolio_views "$2"
    ;;
  *)
    usage
    exit 2
    ;;
esac
