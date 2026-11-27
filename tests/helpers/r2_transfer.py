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
if [[ -n "${LEASE_FAKE_RELEASE_ON:-}" && "$*" == *"${LEASE_FAKE_RELEASE_ON}"* ]]; then
  printf 'synthetic-released' > "$PUBLICATION_LEASE_HANDLE"
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
    else
      object_etag "$slug"
    fi
    exit 0
  fi
  exit 97
fi
if [[ "$1 $2" == "s3api get-object" ]]; then
  key=""
  destination=""
  wants_store_metadata=""
  if_match=""
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--key" ]]; then
      key="$2"
    fi
    if [[ "$1" == "--query" && "$2" == "[ETag, ContentLength]" ]]; then
      wants_store_metadata=1
    fi
    if [[ "$1" == "--if-match" ]]; then
      if_match="$2"
    fi
    destination="$1"
    shift
  done
  slug="$(printf '%s' "$key" | tr / _)"
  stored="${AWS_FAKE_STATE}/put-${slug}"
  if [[ "$key" == baibai.sqlite && -n "$wants_store_metadata" ]]; then
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
  if [[ "$key" == indicators.sqlite && -n "$wants_store_metadata" ]]; then
    version="$(object_etag "$slug")"
    if [[ "$key" == "${AWS_FAKE_CHANGED_KEY:-}" ]]; then
      # Tests change the version on the next HEAD, not this pinned GET.
      version='"etag-'"$(wc -c < "${AWS_FAKE_STATE}/calls-${slug}" | tr -d ' ')"'"'
    fi
    if [[ -f "${AWS_FAKE_STATE}/transient-${slug}" ]]; then
      version='"etag-transient"'
      stored="${AWS_FAKE_STATE}/transient-${slug}"
    fi
    if [[ "$if_match" != "$version" || "$key" == "${AWS_FAKE_FAIL_DOWNLOAD_KEY:-}" ]]; then
      printf 'synthetic-private-value\\n' >&2
      exit 254
    fi
    if [[ -f "$stored" ]]; then cp "$stored" "$destination"; else printf x > "$destination"; fi
    if [[ -f "${AWS_FAKE_STATE}/get-metadata-${slug}" ]]; then
      cat "${AWS_FAKE_STATE}/get-metadata-${slug}"
    else
      printf '%s\\t%s\\n' "$version" "$(wc -c < "$destination")"
    fi
    exit 0
  fi
  exit 97
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
  slug="$(printf '%s' "$key" | tr / _)"
  if [[ "$key" != baibai.sqlite && "$key" != indicators.sqlite ]]; then exit 97; fi
  if [[ "$expected" != "$(object_etag "$slug")" ]]; then
    printf 'PreconditionFailed\\n' >&2
    exit 255
  fi
  cp "$body" "${AWS_FAKE_STATE}/put-${slug}" || exit 97
  printf 'x' >> "${AWS_FAKE_STATE}/generation-${slug}"
  if [[ -n "$wants_etag" && -z "${AWS_FAKE_MISSING_PUT_ETAG:-}" ]]; then
    object_etag "$slug"
  fi
  exit 0
fi
if [[ "$1 $2" == "s3 cp" && "$3" == s3://* ]]; then
  key="${3#s3://}"
  key="${key#*/}"
  slug="$(printf '%s' "$key" | tr / _)"
  stored="${AWS_FAKE_STATE}/put-${slug}"
  # The unpinned legacy read can see B even when both HEADs report A.
  if [[ "$key" == indicators.sqlite && -f "${AWS_FAKE_STATE}/transient-${slug}" ]]; then
    stored="${AWS_FAKE_STATE}/transient-${slug}"
  fi
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
  exit 0
fi
if [[ "$1 $2" == "s3 cp" && "$4" == s3://* ]]; then
  key="${4#s3://}"
  key="${key#*/}"
  if [[ "$key" == "${AWS_FAKE_FAIL_UPLOAD_KEY:-}" ]]; then
    printf 'synthetic-private-value' >&2
    exit 254
  fi
  slug="$(printf '%s' "$key" | tr / _)"
  cp "$3" "${AWS_FAKE_STATE}/put-${slug}"
  printf 'x' >> "${AWS_FAKE_STATE}/generation-${slug}"
  exit 0
fi
exit 97
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    uv = bin_dir / "uv"
    # Python snapshot/merge/lease fixtures for the remaining independent stores.
    uv.write_text(
        """#!/usr/bin/env bash
if [[ -n "${UV_EXPECTED_CWD:-}" && "$PWD" != "$UV_EXPECTED_CWD" ]]; then
  printf 'unexpected uv cwd: %s\n' "$PWD" >&2
  exit 97
fi
script=""
for argument in "$@"; do
  case "${argument}" in
    baibai_engine.storage.sqlite.snapshot) script=snapshot ;;
    baibai_jobs.integration.native_source_merge) script=merge ;;
    baibai_jobs.observability.repository_layout) script=layout ;;
    baibai_jobs.publication.lease) script=lease ;;
  esac
done
case "${script}" in
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
