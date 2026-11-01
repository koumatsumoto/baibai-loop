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
  if [[ -n "$wants_etag" ]]; then
    # Counts the calls per key so a test can make one object change mid-pull:
    # the version query runs once before the downloads and once after.
    calls="${AWS_FAKE_STATE}/calls-${slug}"
    printf 'x' >> "$calls"
    if [[ -n "${AWS_FAKE_ETAG_OVERRIDE:-}" ]]; then
      printf '%s\n' "${AWS_FAKE_ETAG_OVERRIDE}"
    elif [[ "$key" == "${AWS_FAKE_CHANGED_KEY:-}" ]]; then
      printf '"etag-%s"\\n' "$(wc -c < "$calls" | tr -d ' ')"
    elif [[ -s "${AWS_FAKE_STATE}/generation-${slug}" ]]; then
      # An upload changes the object, so every later read of it reads a new version.
      # Without this the fake lets a conditional PUT bound to the pulled generation
      # succeed twice, which no real bucket does.
      printf '"etag-put-%s"\\n' "$(wc -c < "${AWS_FAKE_STATE}/generation-${slug}" | tr -d ' ')"
    else
      printf '"etag-stable"\\n'
    fi
    exit 0
  fi
  if [[ "$key" == "${AWS_FAKE_HEAD_ERROR_KEY:-}" ]]; then
    printf '%s\\n' "${AWS_FAKE_HEAD_ERROR}" >&2
    exit 254
  fi
  if [[ -f "${AWS_FAKE_STATE}/deleted-${slug}" ]]; then
    printf 'An error occurred (404) when calling the HeadObject operation\\n' >&2
    exit 254
  fi
  # Uploaded objects exist from then on. The colon-separated list seeds what the bucket
  # held before the run, so a test can make the bundle receipt exist beside the backup
  # key the push path checks for.
  if [[ -s "${AWS_FAKE_STATE}/generation-${slug}" ]]; then
    printf '{"ContentLength": 1}\\n'
    exit 0
  fi
  for existing in ${AWS_FAKE_EXISTING_KEY:+${AWS_FAKE_EXISTING_KEY//:/ }}; do
    if [[ "$key" == "$existing" ]]; then
      printf '{"ContentLength": 1}\\n'
      exit 0
    fi
  done
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
  # `--query Contents[].Key --output text` prints the keys on one tab-separated line,
  # and `None` when the prefix matches nothing.
  printf '%s\n' "${AWS_FAKE_LIST_KEYS:-None}"
  exit 0
fi
if [[ "$1 $2" == "s3api get-object" ]]; then
  key=""
  destination=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--key" ]]; then
      key="$2"
    fi
    destination="$1"
    shift
  done
  if [[ -n "${AWS_FAKE_OBJECT_BODY:-}" && "$key" == "${AWS_FAKE_OBJECT_KEY:-}" ]]; then
    printf '%s' "$AWS_FAKE_OBJECT_BODY" > "$destination"
    exit 0
  fi
  # Otherwise the bucket answers with what was uploaded to that key, so a test can push
  # and then read the object the push wrote rather than one the test composed for it.
  stored="${AWS_FAKE_STATE}/put-$(printf '%s' "$key" | tr / _)"
  if [[ -f "$stored" ]]; then
    cp "$stored" "$destination"
    exit 0
  fi
  printf 'An error occurred (NoSuchKey) when calling the GetObject operation\\n' >&2
  exit 254
fi
if [[ "$1 $2" == "s3api put-object" && -n "${AWS_FAKE_REJECT_CONDITIONAL_PUT:-}" ]]; then
  printf 'An error occurred (PreconditionFailed) when calling PutObject\n' >&2
  exit 255
fi
if [[ "$1 $2" == "s3api put-object" ]]; then
  key=""
  body=""
  expected=""
  wants_etag=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--key" ]]; then
      key="$2"
    fi
    if [[ "$1" == "--if-match" ]]; then
      expected="$2"
    fi
    if [[ "$1" == "--body" ]]; then
      body="$2"
    fi
    if [[ "$1" == "--query" && "$2" == "ETag" ]]; then
      wants_etag=1
    fi
    shift
  done
  if [[ "$key" == "baibai.sqlite" && -n "$expected" ]]; then
    current='"etag-stable"'
    if [[ -s "${AWS_FAKE_STATE}/generation-${key}" ]]; then
      current="\\\"etag-put-$(wc -c < "${AWS_FAKE_STATE}/generation-${key}" | tr -d ' ')\\\""
    fi
    if [[ "$expected" != "$current" ]]; then
      printf 'PreconditionFailed\\n' >&2
      exit 255
    fi
  fi
  # Inject a rejected PUT: neither its body nor its generation is committed.
  if [[ -n "${AWS_FAKE_FAIL_PUT_KEY:-}" && "$key" == "${AWS_FAKE_FAIL_PUT_KEY}" ]]; then
    printf 'An error occurred (InternalError) when calling PutObject\n' >&2
    exit 253
  fi
  # Every uploaded body is kept so a test can read what was actually published
  # rather than only that a call was made.
  if [[ -n "$body" && -f "$body" ]]; then
    cp "$body" "${AWS_FAKE_STATE}/put-$(printf '%s' "$key" | tr / _)"
  fi
  slug="$(printf '%s' "$key" | tr / _)"
  printf 'x' >> "${AWS_FAKE_STATE}/generation-${slug}"
  rm -f "${AWS_FAKE_STATE}/deleted-${slug}"
  if [[ -n "$wants_etag" && -z "${AWS_FAKE_MISSING_PUT_ETAG:-}" ]]; then
    printf '\"etag-put-%s\"\\n' "$(wc -c < "${AWS_FAKE_STATE}/generation-${slug}" | tr -d ' ')"
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
if [[ "$*" == *"baibai-jobs snapshots dehydrate"* ]]; then
  printf 'dehydrate %s\\n' "$*" >> "$AWS_LOG"
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
  esac
done
case "${script}" in
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
    if [[ -n "${PUBLISH_FAKE_PAYLOAD:-}" ]]; then
      printf '%s\\n' "${PUBLISH_FAKE_PAYLOAD}"
    else
      printf '{"release_id": "release-after-rebuild", "release_manifest_sha256": "%s"}\\n' \\
        "0000000000000000000000000000000000000000000000000000000000000000"
    fi
    exit 0
    ;;
  snapshot)
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
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AWS_LOG": str(log),
        "AWS_FAKE_STATE": str(state),
        "R2_GENERATION_DIR": str(generation_dir),
        "R2_ACCOUNT_ID": "account-for-test",
        "R2_ACCESS_KEY_ID": "access-for-test",
        "R2_SECRET_ACCESS_KEY": "secret-for-test",
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
