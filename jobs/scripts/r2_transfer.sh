#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
stores_bucket="${R2_STORES_BUCKET:-baibai-stores}"
generation_dir="${R2_GENERATION_DIR:-${repo_root}/stores/.r2-generations}"
# The lake mirror is a fetch-through cache of immutable objects, so it is named next to
# the stores rather than inside one: every key it holds begins with `lake/`, which is why
# the mirror root is the directory the stores live in and not one of them.
lake_mirror="${R2_LAKE_MIRROR:-${repo_root}/stores}"
copy_read_timeout=300
# The machine receipt prevents a partial sequential push from looking like a coherent pair.
machine_receipt_key="machine-manifest.json"
machine_bundle_keys=(market.sqlite runs.sqlite)
transfer_staging=""
seed_receipt_dir=""
seed_receipt_verified=0
evidence_dir=""
evidence_pending_key=""
daily_origin=""
measure_machine_upload=0

cleanup_staging() {
  if [[ -n "${seed_receipt_dir}" ]] \
    && [[ "${seed_receipt_verified}" == 1 || ! -f "${seed_receipt_dir}/${machine_receipt_key}" ]]; then
    rm -f -- "${seed_receipt_dir}/${machine_receipt_key}" "${seed_receipt_dir}/.pending"
    rmdir -- "${seed_receipt_dir}"
    seed_receipt_dir=""
  fi
  if [[ -n "${transfer_staging}" && -d "${transfer_staging}" ]]; then
    case "${transfer_staging}" in
      "${repo_root}"/.r2-transfer.*) rm -r -- "${transfer_staging}" ;;
      *) printf 'refusing to remove unexpected staging path: %s\n' "${transfer_staging}" >&2 ;;
    esac
  fi
}
trap cleanup_staging EXIT

transfer_evidence() {
  [[ -n "${evidence_dir}" ]] || return 0
  python3 - "${evidence_dir}" "$1" "${2:-}" "${3:-}" "${4:-}" \
    "${repo_root}" "${PUBLICATION_LEASE_HANDLE:-}" "${evidence_pending_key}" \
    "${transfer_staging}" <<'PYTHON'
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

root, phase, key, expected, ack, repo, handle, pending, staging = sys.argv[1:]
root = Path(root)
handle = Path(handle) if Path(handle).is_absolute() else Path(repo) / handle

def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()

try:
    record = {"phase": phase, "at_utc": datetime.now(timezone.utc).isoformat()}
    if phase == "started":
        lease = json.loads(handle.read_bytes())
        record.update(
            code_commit=subprocess.check_output(
                ["git", "-C", repo, "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
            ).strip(),
            script_sha256=digest(Path(repo) / "jobs/scripts/r2_transfer.sh"),
            lease={name: lease[name] for name in (
                "owner", "purpose", "acquired_at_utc", "expires_at_utc"
            )},
            lease_handle_sha256=digest(handle),
            readback_accepted=False,
        )
        root.mkdir(mode=0o700)
        root.chmod(0o700)
    if key:
        if key not in {"market.sqlite", "runs.sqlite", "machine-manifest.json"}:
            raise ValueError("unexpected evidence key")
        record.update(key=key, if_match=expected or None)
    if phase in {"snapshot_ready", "put_started", "put_ack"}:
        body = root / key
        if phase == "snapshot_ready":
            source = Path(staging) / key
            try:
                os.link(source, body)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                with source.open("rb") as incoming, body.open("xb") as retained:
                    shutil.copyfileobj(incoming, retained)
                    retained.flush()
                    os.fsync(retained.fileno())
                if digest(source) != digest(body):
                    raise ValueError("retained transfer copy differs")
        if body.is_symlink() or not body.is_file():
            raise ValueError("body is not a regular retained file")
        body.chmod(0o600)
        record.update(body=key, sha256=digest(body), size=body.stat().st_size)
        with body.open("rb") as stream:
            os.fsync(stream.fileno())
        if phase != "snapshot_ready":
            ready = json.loads((root / (key + ".snapshot_ready.json")).read_bytes())
            if any(record[name] != ready[name] for name in ("sha256", "size", "if_match")):
                raise ValueError("prepared body or precondition changed")
            start = json.loads((root / "attempt.started.json").read_bytes())
            if digest(handle) != start["lease_handle_sha256"]:
                raise ValueError("publication handle changed")
    if phase == "put_ack":
        record["put_response_etag"] = ack
    if phase in {"stopped", "completed"}:
        record.update(outcome_unknown_key=pending or None, readback_accepted=False)
        if phase == "completed":
            for name in ("market.sqlite", "runs.sqlite", "machine-manifest.json"):
                if not (root / (name + ".put_ack.json")).is_file():
                    raise ValueError("missing PUT acknowledgement")
    path = root / ((key or "attempt") + "." + phase + ".json")
    with path.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError):
    print("transfer evidence could not be recorded (" + phase + ")", file=sys.stderr)
    raise SystemExit(1) from None
PYTHON
}

finish_evidence() {
  local status=$?
  trap - EXIT
  if [[ ${status} -eq 0 ]]; then
    if ! transfer_evidence completed; then
      status=1
      transfer_evidence stopped || true
    fi
  else
    transfer_evidence stopped || true
  fi
  cleanup_staging
  exit "${status}"
}

transfer_body() {
  printf '%s/%s\n' "${evidence_dir:-${transfer_staging}}" "$1"
}

put_with_evidence() {
  local key="$1" expected="$2" response version status
  local -a condition=()
  if [[ -n "${expected}" ]]; then condition+=(--if-match "${expected}"); fi
  transfer_evidence put_started "${key}" "${expected}"
  evidence_pending_key="${key}"
  if response="$(aws s3api put-object \
    --bucket "${stores_bucket}" --key "${key}" --body "$(transfer_body "${key}")" \
    "${condition[@]}" --endpoint-url "${endpoint}" --query ETag --output text 2>/dev/null)"; then
    if ! version="$(etag_body "${response}")"; then
      printf 'machine PUT outcome unknown; evidence retained\n' >&2
      return 1
    fi
  else
    status=$?
    printf 'machine PUT outcome unknown; evidence retained\n' >&2
    return "${status}"
  fi
  transfer_evidence put_ack "${key}" "${expected}" "${version}"
  evidence_pending_key=""
}

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
  # CLI stderr can include private endpoints or response text, not a verified status.
  printf 'cannot determine whether R2 object %s exists (HeadObject failed; details withheld)\n' "$1" >&2
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
  local -a options=()
  # Repack only the runs transfer copy, not the working store or its history.
  if [[ "$3" == "runs.sqlite" ]]; then options+=(--compact); fi
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m baibai_engine.storage.sqlite.snapshot create \
        --source "$1" --output "$2" "${options[@]}"
  )
}

merge_market_store() {
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m baibai_jobs.integration.source_cache_merge --source "$1" --target "$2"
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

hydrate_market() {
  # The mode determines which fixed-release tables the next consumer needs.
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run baibai-jobs snapshots "$1" \
        --mirror "${lake_mirror}" \
        --store "$(store_path market.sqlite)" \
        --bucket "${stores_bucket}"
  )
}

publish_lake() {
  # Build from fixed native inputs and one sealed store, then switch the pointer.
  # Publication refuses when the origin embedded in that same SQLite file differs from
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

verify_publication_lease() {
  (
    cd "${repo_root}" || exit 1
    R2_STORES_BUCKET="${stores_bucket}" UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run python -m baibai_jobs.publication.lease verify \
        --handle "${PUBLICATION_LEASE_HANDLE:?publication lease handle is required}" >/dev/null
  )
}

dehydrate_market_snapshot() {
  # Runs on the upload copy, never on the working store. Origin and row counts must
  # match the release; values are not compared. Publish local changes first and do not
  # edit lake-owned rows between publication and snapshotting.
  local source="$1" path="$2"
  (
    cd "${repo_root}" || exit 1
    UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/baibai-uv-cache}" \
      uv run baibai-jobs snapshots dehydrate \
        --mirror "${lake_mirror}" \
        --source "${source}" --store "${path}" \
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
  # AWS's text query may return a quoted ETag or a null sentinel. Only an actual
  # version can participate in a receipt or its conditional replacement.
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

live_machine_versions() {
  local key version
  for key in "${machine_bundle_keys[@]}"; do
    version="$(remote_version "${key}" 2>/dev/null)" || {
      printf 'cannot read current machine version for %s (details withheld)\n' "${key}" >&2
      return 2
    }
    etag_body "${version}" || return
  done
}

require_live_machine_versions() {
  local actual
  actual="$(live_machine_versions)" || return
  if [[ "${actual}" != "$1" ]]; then
    printf 'live machine versions differ from the proven pair; do not replace the receipt\n' >&2
    return 1
  fi
}

render_machine_receipt() {
  local key file="$1" index=0
  local -a versions
  mapfile -t versions <<< "$2"
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    if [[ -n "${daily_origin}" ]]; then
      printf '  "daily_publication": %s,\n' "${daily_origin}"
    fi
    printf '  "written_at_utc": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '  "keys": {\n'
    for key in "${machine_bundle_keys[@]}"; do
      if [[ ${index} -gt 0 ]]; then
        printf ',\n'
      fi
      printf '    "%s": "%s"' "${key}" "${versions[index]}"
      index=$((index + 1))
    done
    printf '\n  }\n}\n'
  } > "${file}"
}

write_machine_receipt() {
  # Regular publication records the resulting current pair under the lease.
  local file receipt_sha256 versions
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  file="${transfer_staging}/${machine_receipt_key}"
  versions="$(live_machine_versions)"
  render_machine_receipt "${file}" "${versions}"
  transfer_evidence snapshot_ready "${machine_receipt_key}"
  # Pin the small receipt bytes actually being offered, not the SQLite bodies.
  # Print only after a successful PUT so a failed attempt supplies no target.
  receipt_sha256="$(sha256sum < "$(transfer_body "${machine_receipt_key}")")"
  verify_publication_lease
  if [[ -n "${evidence_dir}" ]]; then
    put_with_evidence "${machine_receipt_key}" ""
  else
    aws s3api put-object \
      --bucket "${stores_bucket}" \
      --key "${machine_receipt_key}" \
      --body "${file}" \
      --endpoint-url "${endpoint}" \
      >/dev/null
  fi
  cleanup_staging
  transfer_staging=""
  printf 'machine bundle receipt written: %s machine_receipt_sha256=%s\n' \
    "${machine_receipt_key}" "${receipt_sha256%% *}"
}

recorded_machine_versions() {
  # Read the JSON contract, not the writer's whitespace or line layout.
  python3 - "$1" "${machine_bundle_keys[@]}" <<'PYTHON'
import json
import re
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as source:
        receipt = json.load(source)
except (OSError, ValueError):
    receipt = None
if not isinstance(receipt, dict):
    sys.exit(1)
schema_version = receipt.get("schema_version")
if type(schema_version) is not int or schema_version != 1:
    sys.exit(1)
keys = receipt.get("keys")
if not isinstance(keys, dict):
    sys.exit(1)
versions = [keys.get(key) for key in sys.argv[2:]]
if any(not isinstance(v, str) or v in {"None", "null"} or not re.fullmatch(r"[A-Za-z0-9._:-]+", v)
       for v in versions):
    sys.exit(1)
print(*versions, sep="\n")
PYTHON
}

download_machine_receipt() {
  aws s3api get-object --bucket "${stores_bucket}" --key "${machine_receipt_key}" \
    --endpoint-url "${endpoint}" --query ETag --output text "$1" 2>/dev/null
}

require_machine_receipt_readback() {
  local proof="$1" versions="$2" readback="${transfer_staging}/receipt-readback.json" observed current
  observed="$(download_machine_receipt "${readback}")" || return
  observed="$(etag_body "${observed}")" || return
  cmp -s -- "${proof}" "${readback}" || return
  require_live_machine_versions "${versions}" || return
  current="$(remote_version "${machine_receipt_key}" 2>/dev/null)" || return
  current="$(etag_body "${current}")" || return
  [[ "${observed}" == "${current}" ]]
}

put_verified_machine_receipt() {
  local proof="$1" versions="$2"
  shift 2
  # An absent acknowledgement is not proof that the PUT failed. Accept only the
  # exact candidate/fenced pair on independent readback; never retry the PUT here.
  aws s3api put-object --bucket "${stores_bucket}" --key "${machine_receipt_key}" \
    --body "${proof}" "$@" --endpoint-url "${endpoint}" >/dev/null 2>&1 || true
  if ! require_machine_receipt_readback "${proof}" "${versions}"; then
    printf 'machine bundle receipt write not verified; keep the exact candidate for recovery\n' >&2
    return 1
  fi
}

seed_machine_receipt() {
  local versions="$1" receipt_sha256
  local candidate="${seed_receipt_dir}/${machine_receipt_key}" pending="${seed_receipt_dir}/.pending"
  # Retain exact same-attempt acknowledgement evidence even if the following HEAD fails.
  (umask 077; render_machine_receipt "${pending}" "${versions}") || return
  receipt_sha256="$(sha256sum < "${pending}")" || return
  mv -T -- "${pending}" "${candidate}" || return
  printf 'seed receipt candidate (not yet verified): %s candidate_sha256=%s\n' \
    "${candidate}" "${receipt_sha256%% *}" >&2
  # Bootstrap is outside the lease. Only a pair still live can get a receipt.
  require_live_machine_versions "${versions}" || return
  put_verified_machine_receipt "${candidate}" "${versions}" --if-none-match '*' || return
  seed_receipt_verified=1
  printf 'initial machine bundle receipt verified: %s\n' "${machine_receipt_key}"
}

restore_machine_receipt() {
  local source="$1" target_sha256="$2"
  local proof before receipt_sha256 versions observed current
  local -a condition=(--if-none-match '*')
  verify_publication_lease || return
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")" || return
  proof="${transfer_staging}/${machine_receipt_key}"
  before="${transfer_staging}/receipt-before.json"
  # Copy first, so the bytes we validate are the bytes we will send.
  if [[ ! -f "${source}" ]] || ! cp -- "${source}" "${proof}"; then
    printf 'restore requires a readable retained receipt file\n' >&2
    return 2
  fi
  receipt_sha256="$(sha256sum < "${proof}")" || return
  if [[ "${receipt_sha256%% *}" != "${target_sha256}" ]] \
    || ! versions="$(recorded_machine_versions "${proof}")"; then
    printf 'restore requires the retained schema-v1 receipt with its proven SHA-256\n' >&2
    return 1
  fi
  require_live_machine_versions "${versions}" || return
  if remote_object_exists "${machine_receipt_key}"; then
    observed="$(download_machine_receipt "${before}")" || {
      printf 'cannot read the existing machine receipt (details withheld)\n' >&2
      return 2
    }
    etag_body "${observed}" >/dev/null || return
    if cmp -s -- "${proof}" "${before}"; then
      require_machine_receipt_readback "${proof}" "${versions}" || return
      verify_publication_lease || return
      printf 'exact machine bundle receipt already verified; no write\n'
      return
    fi
    if ! current="$(recorded_machine_versions "${before}")" || [[ "${current}" == "${versions}" ]]; then
      printf 'existing receipt is not provably stale; preserve it and inspect the publication\n' >&2
      return 1
    fi
    condition=(--if-match "${observed}")
  fi
  require_live_machine_versions "${versions}" || return
  verify_publication_lease || return
  put_verified_machine_receipt "${proof}" "${versions}" "${condition[@]}" || return
  verify_publication_lease || return
  printf 'exact machine bundle receipt restored and verified\n'
}

require_machine_receipt() {
  # A mismatch may be an active push or an interrupted one; blind retry cannot fix both.
  local file entry key expected actual versions index=0
  local -a recorded
  file="${transfer_staging}/${machine_receipt_key}"
  if ! remote_object_exists "${machine_receipt_key}"; then
    printf 'refusing to replace local stores: machine bundle receipt is missing. See ' >&2
    printf '"部分 push からの復旧" in jobs/OPERATIONS.md.\n' >&2
    return 1
  fi
  if ! download_machine_receipt "${file}" >/dev/null; then
    printf 'machine bundle receipt could not be downloaded (details withheld)\n' >&2
    return 2
  fi
  if ! versions="$(recorded_machine_versions "${file}")"; then
    printf 'machine bundle receipt names no generation (invalid schema or member)\n' >&2
    return 1
  fi
  mapfile -t recorded <<< "${versions}"
  for entry in "$@"; do
    key="${entry%%=*}"
    actual="$(etag_body "${entry#*=}")"
    expected="${recorded[index]}"
    index=$((index + 1))
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
  local verify_receipt=0 daily_preflight=0
  if [[ "${1:-}" == "--bundle-receipt" ]]; then
    verify_receipt=1
    shift
    if [[ "${1:-}" == "--daily-preflight" ]]; then
      daily_preflight=1
      shift
    fi
  fi
  local key target index
  for key in "$@"; do
    if [[ "${key}" == "baibai.sqlite" ]]; then
      printf 'application store must be fetched with pull-app\n' >&2
      return 2
    fi
  done
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  # Verify all generations around the downloads before replacing local stores.
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
    require_machine_receipt "${observed[@]}"
  fi
  for key in "$@"; do
    aws_s3 cp "s3://${stores_bucket}/${key}" "${transfer_staging}/${key}"
    check_sqlite "${transfer_staging}/${key}"
  done
  if [[ ${daily_preflight} -eq 1 ]]; then
    (cd "${repo_root}" && uv run python -m baibai_jobs.publication.machine_preflight \
      --market "${transfer_staging}/market.sqlite" \
      --receipt "${transfer_staging}/${machine_receipt_key}" --bucket "${stores_bucket}" \
      --lease-handle "${PUBLICATION_LEASE_HANDLE}")
  fi
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
  # One fixed backup for each machine store.
  # Machine data is derived/acquired, but expired upstream history may not be
  # recoverable; this backup is recovery protection, not proof of rebuildability.
  printf '%s.bak\n' "$1"
}

backup_remote_key() {
  # Retain the prior generation: some source history cannot be fetched again.
  # R2 needs direct CopyObject rather than `aws s3 cp`; large copies need a longer timeout.
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
  # Report snapshot and upload times; machine writes also report their backup time.
  local key source body started uploaded_version put_started backup_part upload_parts=""
  local -a snapshot_seconds=() seed_versions=()
  for key in "$@"; do
    source="$(store_path "${key}")"
    body="${transfer_staging}/${key}"
    started="${SECONDS}"
    # The copy that leaves here carries only what the lake does not hold — but only
    # once the lake holds anything. A lake release pointer is where that authority is
    # declared, so it is read rather than assumed: before the first publication (an
    # empty bucket being seeded) the store is still the only copy of those rows, and
    # after it the emptying is mandatory rather than best-effort.
    if [[ "${key}" == "market.sqlite" ]] \
      && remote_object_exists "lake/pointers/market/current.json"; then
      if [[ -n "${evidence_dir}" ]]; then
        dehydrate_market_snapshot "${source}" "${body}" >/dev/null
      else
        dehydrate_market_snapshot "${source}" "${body}"
      fi
    else
      snapshot_sqlite "${source}" "${body}" "${key}"
    fi
    transfer_evidence snapshot_ready "${key}" "${expected_version}"
    snapshot_seconds+=("$((SECONDS - started))")
  done
  local index=0
  for key in "$@"; do
    body="${transfer_staging}/${key}"
    started="${SECONDS}"
    backup_part=""
    if [[ "${key}" != "baibai.sqlite" ]]; then
      if [[ -n "${expected_version}" ]]; then
        verify_publication_lease
        transfer_evidence backup_started "${key}" "${expected_version}"
        backup_remote_key "${key}" "${expected_version}"
        transfer_evidence backup_checked "${key}" "${expected_version}"
      fi
      backup_part=" backup=$((SECONDS - started))s"
    fi
    started="${SECONDS}"
    if [[ -n "${expected_version}" ]]; then
      if [[ "${key}" != "baibai.sqlite" ]]; then verify_publication_lease; fi
      # PutObject's If-Match is checked atomically when R2 commits the object. The
      # earlier read avoids an expensive snapshot after an already-visible race;
      # this condition closes the remaining race through backup and upload.
      if [[ -n "${evidence_dir}" ]]; then
        put_with_evidence "${key}" "${expected_version}"
      else
        if [[ "${measure_machine_upload}" == 1 ]]; then put_started="${SECONDS}"; fi
        uploaded_version="$(aws s3api put-object \
          --bucket "${stores_bucket}" \
          --key "${key}" \
          --body "${body}" \
          --if-match "${expected_version}" \
          --endpoint-url "${endpoint}" \
          --query ETag --output text)"
        if [[ "${measure_machine_upload}" == 1 ]]; then
          printf -v upload_parts ' lease_before_put=%ss conditional_put_cli=%ss' \
            "$((put_started - started))" "$((SECONDS - put_started))"
        fi
      fi
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
      uploaded_version="$(aws s3api put-object \
        --bucket "${stores_bucket}" \
        --key "${key}" \
        --body "${body}" \
        --if-none-match '*' \
        --endpoint-url "${endpoint}" \
        --query ETag --output text)"
      if ! uploaded_version="$(etag_body "${uploaded_version}")"; then
        printf 'initial seed has no usable PUT acknowledgement for %s; no receipt created\n' "${key}" >&2
        return 1
      fi
      if [[ "${key}" == market.sqlite || "${key}" == runs.sqlite ]]; then
        seed_versions+=("${uploaded_version}")
      fi
    fi
    printf 'store push: key=%s bytes=%s snapshot=%ss%s upload=%ss%s\n' \
      "${key}" \
      "$(wc -c < "${body}" | tr -d ' \n')" \
      "${snapshot_seconds[index]}" \
      "${backup_part}" \
      "$((SECONDS - started))" "${upload_parts}"
    index=$((index + 1))
  done
  if [[ -z "${expected_version}" ]]; then
    local pair
    pair="$(printf '%s\n' "${seed_versions[@]}")"
    seed_machine_receipt "${pair}"
  fi
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
  write_machine_receipt
}

seed_keys() {
  local key
  for key in "$@" "${machine_receipt_key}"; do
    if remote_object_exists "${key}"; then
      printf 'refusing initial seed: s3://%s/%s already exists\n' \
        "${stores_bucket}" "${key}" >&2
      return 2
    fi
  done
  seed_receipt_dir="$(mktemp -d "${TMPDIR:-/tmp}/baibai-seed-receipt.XXXXXX")"
  _push_keys "" "$@"
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
  local directory="$1" name
  for name in portfolio-dashboard outcomes latest-proposal proposal-history; do
    [[ -s "${directory}/views/${name}.json" ]] || {
      printf 'missing portfolio view: %s\n' "${name}" >&2
      return 2
    }
  done
  transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
  for name in portfolio-dashboard outcomes latest-proposal proposal-history; do
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
  for name in portfolio-dashboard outcomes latest-proposal proposal-history; do
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
  printf 'portfolio views verified from remote readback: 4\n'
}

usage() {
  printf 'usage: %s {pull-machine|pull-app|pull-market|pull-runs|pull-indicators|seed-all|restore-machine-receipt FILE SHA256|hydrate-market|hydrate-market-for-daily|hydrate-portfolio|publish-lake|push-machine [--evidence-dir NEW_DIR|--daily-notice FILE]|push-runs|push-market|push-indicators [--recheck-source SOURCE]...|push-app|upload-portfolio-views DIR}\n' "$0" >&2
}

validate_command() {
  local command="${1:-}"
  case "$command" in
    push-machine)
      [[ $# -eq 1 || ( $# -eq 3 && ( "$2" == "--daily-notice" || "$2" == "--evidence-dir" ) && -n "$3" && "$3" != -* ) ]]
      ;;
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
    restore-machine-receipt)
      [[ $# -eq 3 && -n "$2" && "$2" != -* && "$3" =~ ^[0-9a-f]{64}$ ]]
      ;;
    pull-machine)
      [[ $# -eq 1 || ( $# -eq 2 && "$2" == "--daily-preflight" && -n "${PUBLICATION_LEASE_HANDLE:-}" ) ]]
      ;;
    pull-app|pull-market|pull-runs|pull-indicators|hydrate-market|hydrate-market-for-daily|hydrate-portfolio|seed-all|publish-lake|push-market|push-runs|push-app)
      [[ $# -eq 1 ]]
      ;;
    *)
      return 1
      ;;
  esac
}

# A mistyped argument must not turn a fixed-release or maintenance request into
# a different operation. Refuse it before running helpers or reading credentials.
validate_command "$@" || { usage; exit 2; }
(cd "${repo_root}" && uv run python -m baibai_jobs.observability.repository_layout)

if [[ "${1:-}" == "push-machine" && "${2:-}" == "--daily-notice" ]]; then
  # Check the producer's successful notice and local lake origin before any remote write.
  daily_origin="$(cd "${repo_root}" && uv run python -m baibai_jobs.publication.machine_origin "$3")"
  [[ -n "${daily_origin}" ]] || { printf 'daily machine origin is absent\n' >&2; exit 1; }
elif [[ "${1:-}" == "push-machine" && "${2:-}" == "--evidence-dir" ]]; then
  evidence_dir="$3"
  umask 077
  transfer_evidence started
  trap finish_evidence EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
fi

load_credentials
if [[ -n "${evidence_dir}" || "$1" == seed-all || "$1" == restore-machine-receipt ]]; then
  # Do not disguise a lost first PUT response as a second conditional failure.
  export AWS_MAX_ATTEMPTS=1 AWS_RETRY_MODE=standard
fi
case "${1:-}" in
  pull-app)
    pull_app
    ;;
  pull-machine)
    pull_keys --bundle-receipt "${@:2}" "${machine_bundle_keys[@]}"
    ;;
  # The market store arrives holding only what the lake does not own; these two are the
  # halves that put the rest in and take it back out. They are separate from the pull and
  # the push because their time and their transfer are worth reporting on their own.
  hydrate-market)
    hydrate_market hydrate
    ;;
  hydrate-market-for-daily)
    hydrate_market hydrate-for-daily
    ;;
  hydrate-portfolio)
    hydrate_market hydrate-portfolio
    ;;
  publish-lake)
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
    seed_keys "${machine_bundle_keys[@]}" indicators.sqlite baibai.sqlite
    ;;
  restore-machine-receipt)
    restore_machine_receipt "$2" "$3"
    ;;
  push-machine)
    if [[ -z "${evidence_dir}" ]]; then measure_machine_upload=1; fi
    verify_publication_lease
    push_pulled_keys "${machine_bundle_keys[@]}"
    ;;
  push-runs)
    # Explicit model-store maintenance; never re-upload unrelated machine stores.
    verify_publication_lease
    push_pulled_keys runs.sqlite
    ;;
  push-market)
    verify_publication_lease
    # A local copy can miss newer cloud rows. Merge them before the conditional PUT.
    transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
    market_version="$(remote_version market.sqlite)"
    aws_s3 cp "s3://${stores_bucket}/market.sqlite" "${transfer_staging}/market.sqlite"
    check_sqlite "${transfer_staging}/market.sqlite"
    merge_market_store "${transfer_staging}/market.sqlite" "$(store_path market.sqlite)"
    cleanup_staging
    transfer_staging=""
    push_key_if_version market.sqlite "${market_version}"
    write_machine_receipt
    ;;
  push-indicators)
    shift
    verify_publication_lease
    # Compare local receipts with fresh canonical before combining original rows.
    # A local same-value recheck may be a real A→B→A change against the cloud.
    # A late or unconfirmed receipt remains local for explicit source recovery.
    transfer_staging="$(mktemp -d "${repo_root}/.r2-transfer.XXXXXX")"
    indicator_version="$(remote_version indicators.sqlite)"
    aws_s3 cp "s3://${stores_bucket}/indicators.sqlite" "${transfer_staging}/indicators.sqlite"
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
