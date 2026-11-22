"""実転送shellを一時checkoutで試すAWS/uv合成環境。実R2へ接続しない。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSFER_SCRIPT = REPO_ROOT / "jobs/scripts/r2_transfer.sh"


def _fake_aws(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "aws.log"
    executable = bin_dir / "aws"
    # Mirrors the real CLI closely enough to catch the two ways an existence check
    # silently stops working: `aws s3 ls` rejects the transfer-only flags this
    # script passes to `aws s3`, and a prefix listing matches sibling keys.
    executable.write_text(
        """#!/usr/bin/env bash
printf "%s\\n" "$*" >> "$AWS_LOG"
object_etag() {
  local generation="${AWS_FAKE_STATE}/generation-$1"
  if [[ -s "$generation" ]]; then
    printf '"etag-put-%s"\\n' "$(wc -c < "$generation" | tr -d ' ')"
  else
    printf '"etag-stable"\\n'
  fi
}
object_exists() {
  [[ ! -f "${AWS_FAKE_STATE}/deleted-$2" ]] || return 1
  [[ ! -s "${AWS_FAKE_STATE}/generation-$2" ]] || return 0
  local existing
  for existing in ${AWS_FAKE_EXISTING_KEY:+${AWS_FAKE_EXISTING_KEY//:/ }}; do
    [[ "$1" != "$existing" ]] || return 0
  done
  return 1
}
if [[ -n "${LEASE_FAKE_RELEASE_ON:-}" && "$*" == *"${LEASE_FAKE_RELEASE_ON}"* ]]; then
  printf 'synthetic-released' > "$PUBLICATION_LEASE_HANDLE"
fi
# Echo the transfer config this invocation would read, so a test can assert the
# setting reached the CLI rather than only that a file was written somewhere.
if [[ -n "${AWS_CONFIG_FILE:-}" && -f "${AWS_CONFIG_FILE}" ]]; then
  cat "${AWS_CONFIG_FILE}"
fi
if [[ "$1 $2" == "s3 ls" ]]; then
  for argument in "$@"; do
    case "$argument" in
      --only-show-errors|--no-progress)
        printf 'aws: [ERROR]: An error occurred (ParamValidation): Unknown options: %s\\n' \\
          "$argument" >&2
        exit 252
        ;;
    esac
  done
fi
if [[ "$1 $2" == "s3api head-object" ]]; then
  key=""
  wants_etag=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--key" ]]; then
      key="$2"
    fi
    if [[ "$1" == "--query" && "$2" == "ETag" ]]; then
      wants_etag=1
    fi
    shift
  done
  slug="$(printf '%s' "$key" | tr / _)"
  if [[ "$key" == "${AWS_FAKE_HEAD_ERROR_KEY:-}" ]] \
    || [[ -n "$wants_etag" && "$key" == "${AWS_FAKE_VERSION_HEAD_ERROR_KEY:-}" ]]; then
    printf '%s\\n' "${AWS_FAKE_HEAD_ERROR}" >&2
    exit 254
  fi
  if [[ -n "$wants_etag" ]]; then
    # Counts the calls per key so a test can make one object change mid-pull:
    # the version query runs once before the downloads and once after.
    calls="${AWS_FAKE_STATE}/calls-${slug}"
    printf 'x' >> "$calls"
    if [[ -n "${AWS_FAKE_ETAG_OVERRIDE:-}" ]]; then
      printf '%s\n' "${AWS_FAKE_ETAG_OVERRIDE}"
    elif [[ "$key" == "${AWS_FAKE_CHANGED_KEY:-}" ]]; then
      printf '"etag-%s"\\n' "$(wc -c < "$calls" | tr -d ' ')"
    else
      object_etag "$slug"
    fi
    exit 0
  fi
  # Uploaded objects exist from then on. The colon-separated list seeds what the bucket
  # held before the run, so a test can make the bundle receipt exist beside the backup
  # key the push path checks for.
  if object_exists "$key" "$slug"; then
    printf '{"ContentLength": 1}\\n'
    exit 0
  fi
  printf 'An error occurred (404) when calling the HeadObject operation\\n' >&2
  exit 254
fi
if [[ "$1 $2" == "s3api delete-object" ]]; then
  key=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--key" ]]; then
      key="$2"
    fi
    shift
  done
  slug="$(printf '%s' "$key" | tr / _)"
  printf 'x' > "${AWS_FAKE_STATE}/deleted-${slug}"
  rm -f "${AWS_FAKE_STATE}/generation-${slug}" "${AWS_FAKE_STATE}/put-${slug}"
  exit 0
fi
if [[ "$1 $2" == "s3api list-objects-v2" ]]; then
  [[ " $* " == *" --output json "* && " $* " != *" --query "* ]] || exit 2
  [[ " $* " == *" --no-paginate "* ]] || exit 2
  for argument in "$@"; do
    case "$argument" in
      '{"ContinuationToken": "-next+/%="}')
        [[ -v AWS_FAKE_NEXT_LIST_OUTPUT ]] || exit 2
        printf '%s\\n' "${AWS_FAKE_NEXT_LIST_OUTPUT}"
        exit "${AWS_FAKE_NEXT_LIST_EXIT:-0}" ;;
      *ContinuationToken*|--continuation-token*) exit 2 ;;
    esac
  done
  if [[ -v AWS_FAKE_LIST_OUTPUT ]]; then
    printf '%s\\n' "${AWS_FAKE_LIST_OUTPUT}"
  else
    printf '%s\\n' '{"Prefix":"baibai.sqlite.bak-","KeyCount":0,"IsTruncated":false}'
  fi
  exit "${AWS_FAKE_LIST_EXIT:-0}"
fi
if [[ "$1 $2" == "s3api get-object" ]]; then
  key=""
  destination=""
  wants_etag=""
  wants_app_metadata=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--key" ]]; then
      key="$2"
    fi
    if [[ "$1" == "--query" && "$2" == "ETag" ]]; then
      wants_etag=1
    fi
    if [[ "$1" == "--query" && "$2" == "[ETag, ContentLength]" ]]; then
      wants_app_metadata=1
    fi
    destination="$1"
    shift
  done
  slug="$(printf '%s' "$key" | tr / _)"
  stored="${AWS_FAKE_STATE}/put-${slug}"
  if [[ "$key" == baibai.sqlite && -n "$wants_app_metadata" ]]; then
    if [[ -n "${AWS_FAKE_APP_GET_ERROR:-}" ]]; then
      printf 'synthetic-private-value' >&2
      exit 254
    fi
    if [[ -f "$stored" ]]; then cp "$stored" "$destination"; else printf x > "$destination"; fi
    if [[ -v AWS_FAKE_APP_GET_METADATA ]]; then
      printf '%s\n' "$AWS_FAKE_APP_GET_METADATA"
    else
      printf '%s\t%s\n' "${AWS_FAKE_APP_GET_ETAG:-$(object_etag "$slug")}" \
        "$(wc -c < "$destination")"
    fi
    exit 0
  fi
  if [[ -n "${AWS_FAKE_OBJECT_BODY:-}" && "$key" == "${AWS_FAKE_OBJECT_KEY:-}" ]]; then
    printf '%s' "$AWS_FAKE_OBJECT_BODY" > "$destination"
  # Otherwise the bucket answers with what was uploaded to that key, so a test can push
  # and then read the object the push wrote rather than one the test composed for it.
  elif [[ -f "$stored" ]]; then
    cp "$stored" "$destination"
  else
    printf 'An error occurred (NoSuchKey) when calling the GetObject operation\\n' >&2
    exit 254
  fi
  if [[ -n "$wants_etag" ]]; then object_etag "$slug"; fi
  exit 0
fi
if [[ "$1 $2" == "s3api put-object" && -n "${AWS_FAKE_REJECT_CONDITIONAL_PUT:-}" ]]; then
  printf 'An error occurred (PreconditionFailed) when calling PutObject\n' >&2
  exit 255
fi
if [[ "$1 $2" == "s3api put-object" ]]; then
  key=""
  body=""
  expected=""
  create=""
  wants_etag=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--key" ]]; then
      key="$2"
    fi
    if [[ "$1" == "--if-match" ]]; then
      expected="$2"
    fi
    if [[ "$1" == "--if-none-match" && "$2" == '*' ]]; then
      create=1
    fi
    if [[ "$1" == "--body" ]]; then
      body="$2"
    fi
    if [[ "$1" == "--query" && "$2" == "ETag" ]]; then
      wants_etag=1
    fi
    shift
  done
  slug="$(printf '%s' "$key" | tr / _)"
  if [[ "$key" == machine-manifest.json && -n "${AWS_FAKE_CONCURRENT_RECEIPT_BODY:-}" ]]; then
    printf '%s' "$AWS_FAKE_CONCURRENT_RECEIPT_BODY" > "${AWS_FAKE_STATE}/put-${slug}"
    printf 'x' >> "${AWS_FAKE_STATE}/generation-${slug}"
  fi
  if { [[ -n "$create" ]] && object_exists "$key" "$slug"; } \
    || { [[ "$key" == baibai.sqlite || "$key" == machine-manifest.json ]] \
      && [[ -n "$expected" && "$expected" != "$(object_etag "$slug")" ]]; }; then
    printf 'PreconditionFailed\\n' >&2
    exit 255
  fi
  # Inject a rejected PUT: neither its body nor its generation is committed.
  if [[ -n "${AWS_FAKE_FAIL_PUT_KEY:-}" && "$key" == "${AWS_FAKE_FAIL_PUT_KEY}" ]]; then
    printf 'An error occurred (InternalError) when calling PutObject\n' >&2
    exit 253
  fi
  # Every uploaded body is kept so a test can read what was actually published
  # rather than only that a call was made.
  if [[ -n "$body" && -f "$body" ]]; then
    cp "$body" "${AWS_FAKE_STATE}/put-${slug}"
  fi
  printf 'x' >> "${AWS_FAKE_STATE}/generation-${slug}"
  rm -f "${AWS_FAKE_STATE}/deleted-${slug}"
  if [[ "$key" == "${AWS_FAKE_LOST_PUT_ACK_KEY:-}" ]]; then
    printf 'PutObject response lost\\n' >&2
    exit 253
  fi
  if [[ -n "$wants_etag" && -z "${AWS_FAKE_MISSING_PUT_ETAG:-}" ]]; then
    if [[ "$key" == "${AWS_FAKE_BAD_PUT_ETAG_KEY:-}" ]]; then
      printf '"None"\\n'
    else
      object_etag "$slug"
    fi
  fi
  exit 0
fi
if [[ "$1 $2" == "s3 cp" && "$3" == s3://* ]]; then
  key="${3#s3://}"
  key="${key#*/}"
  stored="${AWS_FAKE_STATE}/put-$(printf '%s' "$key" | tr / _)"
  if [[ "$key" == "${AWS_FAKE_FAIL_DOWNLOAD_KEY:-}" ]]; then
    printf 'synthetic-private-value' >&2
    exit 254
  elif [[ "$key" == "${AWS_FAKE_CORRUPT_DOWNLOAD_KEY:-}" ]]; then
    printf 'corrupted-private-payload' > "$4"
  elif [[ -f "$stored" ]]; then
    cp "$stored" "$4"
  else
    printf 'x' > "$4"
  fi
fi
if [[ "$1 $2" == "s3 cp" && "$4" == s3://* ]]; then
  key="${4#s3://}"
  key="${key#*/}"
  slug="$(printf '%s' "$key" | tr / _)"
  cp "$3" "${AWS_FAKE_STATE}/put-${slug}"
  printf 'x' >> "${AWS_FAKE_STATE}/generation-${slug}"
  rm -f "${AWS_FAKE_STATE}/deleted-${slug}"
fi
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    uv = bin_dir / "uv"
    # Stands in for the Python helpers the script shells out to: the snapshot tool
    # (create writes the file, check only validates) and the two store merges, whose
    # invocations are logged so a test can assert they precede the upload. The merges are
    # matched as modules because the script runs them with `-m`; a file suffix would stop
    # matching the moment they are invoked the way the shared core requires.
    uv.write_text(
        """#!/usr/bin/env bash
if [[ -n "${UV_EXPECTED_CWD:-}" && "$PWD" != "$UV_EXPECTED_CWD" ]]; then
  printf 'unexpected uv cwd: %s\n' "$PWD" >&2
  exit 97
fi
if [[ "$1 $2 $3" == "run python -c" ]]; then
  shift
  exec "$@"
fi
if [[ "$1 $2 $3 $4" == "run baibai-engine portfolio ledger" ]]; then exit 0; fi
if [[ "$1 $2 $3 $4" == "run python -m baibai_jobs.publication.machine_origin" \
   || "$1 $2 $3 $4" == "run python -m baibai_jobs.maintenance.application_backup_retention" ]]; then
  shift 3
  exec python3 -m "$@"
fi
if [[ "$*" == *"baibai-jobs snapshots dehydrate"* ]]; then
  printf 'dehydrate %s\\n' "$*" >> "$AWS_LOG"
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--store" ]]; then printf x > "$2"; fi
    shift
  done
  exit 0
fi
if [[ "$*" == *"baibai-jobs snapshots hydrate"* ]]; then
  printf 'hydrate %s\\n' "$*" >> "$AWS_LOG"
  exit 0
fi
script=""
for argument in "$@"; do
  case "${argument}" in
    baibai_engine.storage.sqlite.snapshot) script=snapshot ;;
    baibai_jobs.integration.native_source_merge) script=merge ;;
    baibai_jobs.integration.source_cache_merge) script=merge ;;
    baibai_jobs.observability.repository_layout) script=layout ;;
    baibai_jobs.publication.snapshot_publication) script=publish ;;
    baibai_jobs.publication.lease) script=lease ;;
    baibai_jobs.maintenance.application_backup) script=app_backup ;;
  esac
done
case "${script}" in
  app_backup)
    printf 'app-backup %s\\n' "$*" >> "$AWS_LOG"
    exit "${APP_BACKUP_FAKE_EXIT:-0}"
    ;;
  lease)
    printf 'verify %s\\n' "$R2_STORES_BUCKET" >> "$AWS_FAKE_STATE/lease-checks"
    handle=""
    while [[ $# -gt 0 ]]; do
      if [[ "$1" == "--handle" ]]; then handle="$2"; fi
      shift
    done
    # Sentinel state only; the actual CLI/owner validation is tested separately.
    if [[ -z "$handle" || ! -f "$handle" || "$(cat "$handle")" != synthetic-held ]]; then
      printf 'publication lease: failure (LeaseError)\\n' >&2
      exit 1
    fi
    exit 0
    ;;
  publish)
    printf 'publish %s\\n' "$*" >> "$AWS_LOG"
    # The real publisher prints its JSON report only on success. Refusal may still have
    # emitted partial stdout, but the wrapper persists none of it as identity state.
    if [[ "${PUBLISH_FAKE_EXIT:-0}" != "0" ]]; then
      if [[ -n "${PUBLISH_FAKE_PARTIAL:-}" ]]; then
        printf '%s' "${PUBLISH_FAKE_PARTIAL}"
      fi
      printf 'error: publish refused\\n' >&2
      exit "${PUBLISH_FAKE_EXIT}"
    fi
    printf '{"release_id": "release-after-rebuild", "release_manifest_sha256": "%s"}\\n' \\
      "0000000000000000000000000000000000000000000000000000000000000000"
    exit 0
    ;;
  snapshot)
    if [[ -n "${SNAPSHOT_LOG:-}" ]]; then printf '%s\\n' "$*" >> "$SNAPSHOT_LOG"; fi
    if [[ "$*" == *"--compact"* && "${SNAPSHOT_COMPACT_FAKE_EXIT:-0}" != "0" ]]; then
      exit "$SNAPSHOT_COMPACT_FAKE_EXIT"
    fi
    source=""
    output=""
    while [[ $# -gt 0 ]]; do
      if [[ "$1" == "--source" ]]; then
        shift
        source="$1"
      fi
      if [[ "$1" == "--output" ]]; then
        shift
        output="$1"
      fi
      shift
    done
    if [[ -n "$output" ]]; then
      if [[ -n "${SNAPSHOT_FAKE_COPY:-}" ]]; then
        cp "$source" "$output"
      else
        printf 'x' > "$output"
      fi
    fi
    exit 0
    ;;
  merge)
    printf 'merge %s\\n' "$*" >> "$AWS_LOG"
    exit "${MERGE_FAKE_EXIT:-0}"
    ;;
  layout)
    exit "${LAYOUT_FAKE_EXIT:-0}"
    ;;
  *)
    exit 2
    ;;
esac
""",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return bin_dir, log


def _environment(bin_dir: Path, log: Path) -> dict[str, str]:
    state = bin_dir.parent / "aws-state"
    state.mkdir(exist_ok=True)
    generation_dir = bin_dir.parent / "r2-generations"
    generation_dir.mkdir(exist_ok=True)
    for key in ("market.sqlite", "runs.sqlite", "indicators.sqlite"):
        (generation_dir / f"{key}.etag").write_text('"etag-stable"\n', encoding="utf-8")
    handle = state / "publication-lease-handle.json"
    handle.write_text("synthetic-held", encoding="utf-8")
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AWS_LOG": str(log),
        "AWS_FAKE_STATE": str(state),
        "R2_GENERATION_DIR": str(generation_dir),
        "R2_ACCOUNT_ID": "account-for-test",
        "R2_ACCESS_KEY_ID": "access-for-test",
        "R2_SECRET_ACCESS_KEY": "secret-for-test",
        "PUBLICATION_LEASE_HANDLE": str(handle),
    }
    environment.pop("GITHUB_ACTIONS", None)
    return environment


def _fake_repo(tmp_path: Path) -> Path:
    """A repository root the transfer script can write stores into.

    The script resolves every store path from its own location, so a pull run from
    the real checkout writes to the operational stores. Copying `jobs/scripts/` into
    a throwaway root lets a pull actually run and be inspected.
    """
    root = tmp_path / "repo"
    (root / "jobs/scripts").mkdir(parents=True)
    shutil.copy(TRANSFER_SCRIPT, root / "jobs/scripts/r2_transfer.sh")
    (root / "jobs/scripts/r2_transfer.sh").chmod(0o755)
    return root
