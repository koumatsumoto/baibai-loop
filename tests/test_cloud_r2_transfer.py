from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSFER_SCRIPT = REPO_ROOT / "tools/cloud/r2_transfer.sh"


def _compressed_puts(commands: list[str]) -> list[str]:
    return [command for command in commands if command.startswith("s3api put-object ")]


def _remote_writes(commands: list[str]) -> list[str]:
    writes = [
        command
        for command in commands
        if command.startswith(("s3api put-object ", "s3api copy-object "))
    ]
    writes.extend(
        command
        for command in commands
        if command.startswith("s3 cp ")
        and " s3://baibai-stores/" in command
        and not command.startswith("s3 cp s3://")
    )
    return writes


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
head_for_key() {
  local key="$1"
  local stored="${AWS_FAKE_STATE_DIR}/${key}.head"
  if [[ -f "${stored}" ]]; then
    cat "${stored}"
    return 0
  fi
  if [[ -n "${AWS_FAKE_EXISTING_KEY:-}" && "${key}" == "$AWS_FAKE_EXISTING_KEY" ]]; then
    printf '{"LastModified":"2026-07-31T00:00:00.123456Z","ETag":"0123456789abcdef0123456789abcdef","ContentLength":1,"Metadata":{}}\\n'
    return 0
  fi
  return 1
}
write_uploaded_head() {
  local key="$1"
  local metadata="$2"
  local object="$3"
  local head_path="${AWS_FAKE_STATE_DIR}/${key}.head"
  local etag
  etag="$(sha256sum "${object}" | cut -c1-32)"
  mkdir -p "$(dirname "${head_path}")"
  printf '%s' "${etag}" > "${AWS_FAKE_STATE_DIR}/${key}.etag"
  printf '{"LastModified":"2026-07-31T00:01:00Z","ETag":"%s","ContentLength":1,"Metadata":{' "${etag}" > "${head_path}"
  local separator=""
  local item name value
  IFS=',' read -ra items <<< "${metadata}"
  for item in "${items[@]}"; do
    [[ -n "${item}" ]] || continue
    name="${item%%=*}"
    value="${item#*=}"
    printf '%s"%s":"%s"' "${separator}" "${name}" "${value}" >> "${head_path}"
    separator=,
  done
  printf '}}\\n' >> "${head_path}"
}
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
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--key" ]]; then
      key="$2"
    fi
    shift
  done
  if [[ -n "${AWS_FAKE_HEAD_ERROR_KEY:-}" && "${key}" == "$AWS_FAKE_HEAD_ERROR_KEY" ]]; then
    printf 'An error occurred (AccessDenied) when calling the HeadObject operation\n' >&2
    exit 254
  fi
  if head_for_key "${key}"; then
    exit 0
  fi
  printf 'An error occurred (404) when calling the HeadObject operation\\n' >&2
  exit 254
fi
if [[ "$1 $2" == "s3api put-object" ]]; then
  key=""
  body=""
  metadata=""
  expected_etag=""
  expect_absent=false
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --key) key="$2"; shift ;;
      --body) body="$2"; shift ;;
      --metadata) metadata="$2"; shift ;;
      --if-match) expected_etag="$2"; shift ;;
      --if-none-match) expect_absent=true; shift ;;
    esac
    shift
  done
  current_etag=""
  if [[ -f "${AWS_FAKE_STATE_DIR}/${key}.etag" ]]; then
    current_etag="$(<"${AWS_FAKE_STATE_DIR}/${key}.etag")"
  elif [[ -n "${AWS_FAKE_EXISTING_KEY:-}" && "${key}" == "$AWS_FAKE_EXISTING_KEY" ]]; then
    current_etag=0123456789abcdef0123456789abcdef
  fi
  if [[ -n "${AWS_FAKE_CONFLICT_PUT_KEY:-}" && "${key}" == "$AWS_FAKE_CONFLICT_PUT_KEY" ]]; then
    current_etag=ffffffffffffffffffffffffffffffff
  fi
  if [[ "${expect_absent}" == true && -n "${current_etag}" ]]; then
    printf 'An error occurred (PreconditionFailed) when calling PutObject\n' >&2
    exit 254
  fi
  if [[ -n "${expected_etag}" && "${current_etag}" != "${expected_etag}" ]]; then
    printf 'An error occurred (PreconditionFailed) when calling PutObject\n' >&2
    exit 254
  fi
  object="${AWS_FAKE_STATE_DIR}/${key}.object"
  mkdir -p "$(dirname "${object}")"
  cp "${body}" "${object}"
  write_uploaded_head "${key}" "${metadata}" "${object}"
  failure_marker="${AWS_FAKE_STATE_DIR}/${key}.failed-after-commit"
  if [[ -n "${AWS_FAKE_FAIL_AFTER_COMMIT_KEY:-}" \
    && "${key}" == "$AWS_FAKE_FAIL_AFTER_COMMIT_KEY" \
    && ! -f "${failure_marker}" ]]; then
    printf 'failed after remote commit\n' >&2
    printf 'failed' > "${failure_marker}"
    exit 255
  fi
  exit 0
fi
if [[ "$1 $2" == "s3api copy-object" ]]; then
  key=""
  source_key=""
  expected_etag=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --key) key="$2"; shift ;;
      --copy-source) source_key="${2#*/}"; shift ;;
      --copy-source-if-match) expected_etag="$2"; shift ;;
    esac
    shift
  done
  source_etag=""
  [[ -f "${AWS_FAKE_STATE_DIR}/${source_key}.etag" ]] && \
    source_etag="$(<"${AWS_FAKE_STATE_DIR}/${source_key}.etag")"
  if [[ -n "${expected_etag}" && "${source_etag}" != "${expected_etag}" ]]; then
    printf 'An error occurred (PreconditionFailed) when calling CopyObject\n' >&2
    exit 254
  fi
  if [[ -f "${AWS_FAKE_STATE_DIR}/${source_key}.object" ]]; then
    destination="${AWS_FAKE_STATE_DIR}/${key}.object"
    mkdir -p "$(dirname "${destination}")"
    cp "${AWS_FAKE_STATE_DIR}/${source_key}.object" "${destination}"
    cp "${AWS_FAKE_STATE_DIR}/${source_key}.head" "${AWS_FAKE_STATE_DIR}/${key}.head"
    cp "${AWS_FAKE_STATE_DIR}/${source_key}.etag" "${AWS_FAKE_STATE_DIR}/${key}.etag"
  fi
  exit 0
fi
if [[ "$1 $2" == "s3 cp" ]]; then
  source_path="$3"
  destination="$4"
  if [[ "${source_path}" == s3://baibai-stores/* ]]; then
    key="${source_path#s3://baibai-stores/}"
    object="${AWS_FAKE_STATE_DIR}/${key}.object"
    if [[ -f "${object}" ]]; then
      cp "${object}" "${destination}"
    else
      printf 'x' > "${destination}"
    fi
    exit 0
  fi
  if [[ "${destination}" == s3://baibai-stores/* ]]; then
    key="${destination#s3://baibai-stores/}"
    object="${AWS_FAKE_STATE_DIR}/${key}.object"
    mkdir -p "$(dirname "${object}")"
    cp "${source_path}" "${object}"
    metadata=""
    while [[ $# -gt 0 ]]; do
      if [[ "$1" == "--metadata" ]]; then
        metadata="$2"
        break
      fi
      shift
    done
    write_uploaded_head "${key}" "${metadata}" "${object}"
    exit 0
  fi
  if [[ "${source_path}" == s3://* ]]; then
    printf 'x' > "${destination}"
  fi
fi
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    uv = bin_dir / "uv"
    # Stands in for the two Python helpers the script shells out to: the snapshot tool
    # (create writes the file, check only validates) and the indicator-store merge, whose
    # invocation is logged so a test can assert it precedes the upload.
    uv.write_text(
        """#!/usr/bin/env bash
script=""
for argument in "$@"; do
  case "${argument}" in
    *sqlite_snapshot.py) script=snapshot ;;
    *sqlite_transport.py) script=transport ;;
    *merge_indicator_store.py) script=merge ;;
  esac
done
case "${script}" in
  snapshot)
    if [[ "$*" == *" version "* ]]; then
      printf '%s\\n' "${SQLITE_FAKE_VERSION:-13}"
      exit 0
    fi
    while [[ $# -gt 0 ]]; do
      if [[ "$1" == "--output" ]]; then
        shift
        if [[ -n "${SQLITE_FAKE_FAIL_BASENAME:-}" && "$(basename "$1")" == "$SQLITE_FAKE_FAIL_BASENAME" ]]; then
          exit 9
        fi
        printf '%s' "${SQLITE_FAKE_SNAPSHOT_CONTENT:-x}" > "$1"
      fi
      shift
    done
    exit 0
    ;;
  merge)
    printf 'merge %s\\n' "$*" >> "$AWS_LOG"
    if [[ "${MERGE_FAKE_MUTATE_RAW_HEAD:-false}" == true ]]; then
      mkdir -p "${AWS_FAKE_STATE_DIR}"
      printf '{"LastModified":"2026-07-31T00:02:00Z","ETag":"fedcba9876543210fedcba9876543210","ContentLength":1,"Metadata":{}}\\n' > "${AWS_FAKE_STATE_DIR}/macro.sqlite.head"
    fi
    exit "${MERGE_FAKE_EXIT:-0}"
    ;;
  transport)
    while [[ "$1" != *sqlite_transport.py ]]; do shift; done
    exec "$PYTHON_FOR_TEST" "$@"
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
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AWS_LOG": str(log),
        "AWS_FAKE_STATE_DIR": str(log.parent / "aws-state"),
        "PYTHON_FOR_TEST": sys.executable,
        "R2_ACCOUNT_ID": "account-for-test",
        "R2_ACCESS_KEY_ID": "access-for-test",
        "R2_SECRET_ACCESS_KEY": "secret-for-test",
    }
    environment.pop("GITHUB_ACTIONS", None)
    return environment


def _isolated_transfer_script(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "isolated-repo"
    cloud_tools = repo_root / "tools/cloud"
    cloud_tools.mkdir(parents=True)
    for name in ("r2_transfer.sh", "sqlite_transport.py"):
        shutil.copy2(REPO_ROOT / "tools/cloud" / name, cloud_tools / name)
    return repo_root, cloud_tools / "r2_transfer.sh"


def test_upload_serving_replaces_views_appends_history_and_writes_meta_last(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    output = tmp_path / "serving"
    (output / "views").mkdir(parents=True)
    (output / "views/dashboard.json").write_text("{}", encoding="utf-8")
    (output / "views/meta.json").write_text("{}", encoding="utf-8")
    (output / "history/candidate-views").mkdir(parents=True)
    (output / "history/candidate-views/2026-07-21.json").write_text("{}", encoding="utf-8")

    subprocess.run(
        [TRANSFER_SCRIPT, "upload-serving", output],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=True,
    )

    commands = log.read_text(encoding="utf-8").splitlines()
    assert len(commands) == 3
    assert commands[0].startswith("s3 sync ")
    assert "s3://baibai-serving/views/" in commands[0]
    assert "--delete --exclude meta.json" in commands[0]
    assert "s3://baibai-serving/history/candidate-views/" in commands[1]
    assert "--delete" not in commands[1]
    assert commands[2].startswith("s3 cp ")
    assert commands[2].endswith(
        "s3://baibai-serving/views/meta.json --endpoint-url "
        "https://account-for-test.r2.cloudflarestorage.com --only-show-errors --no-progress"
    )


def test_upload_serving_rejects_an_export_without_meta(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    output = tmp_path / "serving"
    (output / "views").mkdir(parents=True)

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "upload-serving", output],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "meta.json is missing" in completed.stderr
    assert not log.exists()


def test_run_summary_upload_writes_one_object_outside_the_views_prefix(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    summary = tmp_path / "workflow-run-summary.json"
    summary.write_text('{"schema_version": 1}', encoding="utf-8")

    subprocess.run(
        [TRANSFER_SCRIPT, "upload-run-summary", summary],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=True,
    )

    commands = log.read_text(encoding="utf-8").splitlines()
    assert len(commands) == 1
    assert commands[0].startswith("s3 cp ")
    # Not under views/, which `upload-serving` mirrors with --delete.
    assert "s3://baibai-serving/system/latest-run.json" in commands[0]


def test_run_summary_upload_reports_a_missing_summary_without_uploading(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "upload-run-summary", tmp_path / "absent.json"],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "no workflow run summary to upload" in completed.stderr
    assert not log.exists()


def test_preserve_market_v13_uploads_once_and_verifies_download(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "preserve-market-v13"],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    commands = log.read_text(encoding="utf-8").splitlines()
    key = "s3://baibai-stores/schema-migrations/market-v13.sqlite"
    assert sum(key in command and command.startswith("s3 cp ") for command in commands) == 2


def test_preserve_market_v13_refuses_v14_without_rollback_object(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["SQLITE_FAKE_VERSION"] = "14"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "preserve-market-v13"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "required immutable rollback object is missing" in completed.stderr
    assert _remote_writes(log.read_text(encoding="utf-8").splitlines()) == []


def test_preserve_market_v13_reuses_existing_immutable_object(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["SQLITE_FAKE_VERSION"] = "14"
    env["AWS_FAKE_EXISTING_KEY"] = "schema-migrations/market-v13.sqlite"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "preserve-market-v13"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    commands = log.read_text(encoding="utf-8").splitlines()
    uploads = [
        command
        for command in commands
        if command.startswith("s3 cp ")
        and command.rstrip().endswith(
            "s3://baibai-stores/schema-migrations/market-v13.sqlite --endpoint-url "
            "https://account-for-test.r2.cloudflarestorage.com --only-show-errors --no-progress"
        )
    ]
    assert uploads == []


def test_preserve_market_v13_accepts_future_schema_with_existing_artifact(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["SQLITE_FAKE_VERSION"] = "15"
    env["AWS_FAKE_EXISTING_KEY"] = "schema-migrations/market-v13.sqlite"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "preserve-market-v13"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0


def test_download_market_v13_rollback_is_read_only_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    output = tmp_path / "rollback.sqlite"

    first = subprocess.run(
        [TRANSFER_SCRIPT, "download-market-v13-rollback", output],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        [TRANSFER_SCRIPT, "download-market-v13-rollback", output],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert first.returncode == 0
    assert output.read_text(encoding="utf-8") == "x"
    assert second.returncode == 2
    assert "refusing rollback download overwrite" in second.stderr
    commands = log.read_text(encoding="utf-8").splitlines()
    assert all(
        not command.rstrip().endswith(
            "s3://baibai-stores/market.sqlite --endpoint-url "
            "https://account-for-test.r2.cloudflarestorage.com --only-show-errors --no-progress"
        )
        for command in commands
    )


def test_pull_market_accepts_a_raw_only_compatibility_object(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    repo_root, script = _isolated_transfer_script(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = "market.sqlite"

    subprocess.run(
        [script, "pull-market"],
        cwd=repo_root,
        env=env,
        check=True,
    )

    assert (repo_root / "data/screening/market.sqlite").read_bytes() == b"x"


def test_pull_market_accepts_a_compressed_only_object(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    repo_root, script = _isolated_transfer_script(tmp_path)
    env = _environment(bin_dir, log)
    subprocess.run([script, "seed-all"], cwd=repo_root, env=env, check=True)

    subprocess.run(
        [script, "pull-market"],
        cwd=repo_root,
        env=env,
        check=True,
    )

    assert (repo_root / "data/screening/market.sqlite").read_bytes() == b"x"


def test_pull_market_fails_if_retained_raw_disappears_after_migration(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    repo_root, script = _isolated_transfer_script(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = "market.sqlite"
    env["GITHUB_ACTIONS"] = "true"
    subprocess.run([script, "push-market"], cwd=repo_root, env=env, check=True)
    env.pop("AWS_FAKE_EXISTING_KEY")
    local_store = repo_root / "data/screening/market.sqlite"
    local_store.parent.mkdir(parents=True, exist_ok=True)
    local_store.write_bytes(b"local-before-pull")

    completed = subprocess.run(
        [script, "pull-market"],
        cwd=repo_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "retained raw object disappeared" in completed.stderr
    assert local_store.read_bytes() == b"local-before-pull"


def test_pull_all_keeps_every_local_store_when_one_compressed_body_is_corrupt(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    repo_root, script = _isolated_transfer_script(tmp_path)
    env = _environment(bin_dir, log)
    subprocess.run([script, "seed-all"], cwd=repo_root, env=env, check=True)
    (tmp_path / "aws-state/macro.sqlite.zst.object").write_bytes(b"corrupt")
    local_paths = [
        repo_root / "data/screening/market.sqlite",
        repo_root / "data/screening/runs.sqlite",
        repo_root / "data/indicators/macro.sqlite",
        repo_root / "data/app/baibai.sqlite",
    ]
    for index, path in enumerate(local_paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"local-{index}".encode())

    completed = subprocess.run(
        [script, "pull-all"],
        cwd=repo_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    for index, path in enumerate(local_paths):
        assert path.read_bytes() == f"local-{index}".encode()


def test_pull_publish_guard_rejects_remote_change_during_batch_work(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    repo_root, script = _isolated_transfer_script(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    env["R2_PUBLISH_GUARD_DIR"] = str(tmp_path / "publish-guard")
    subprocess.run([script, "seed-all"], cwd=repo_root, env=env, check=True)
    subprocess.run([script, "pull-market"], cwd=repo_root, env=env, check=True)
    head = tmp_path / "aws-state/market.sqlite.zst.head"
    head_payload = json.loads(head.read_text(encoding="utf-8"))
    head_payload["ETag"] = "fedcba9876543210fedcba9876543210"
    head.write_text(json.dumps(head_payload), encoding="utf-8")
    log.unlink()

    completed = subprocess.run(
        [script, "push-market"],
        cwd=repo_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "remote object changed" in completed.stderr
    assert _remote_writes(log.read_text(encoding="utf-8").splitlines()) == []


def test_initial_seed_refuses_to_overwrite_an_existing_store(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = "runs.sqlite"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "seed-all"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "runs.sqlite already exists" in completed.stderr
    assert _remote_writes(log.read_text(encoding="utf-8").splitlines()) == []


def test_initial_seed_refuses_an_existing_compressed_store(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = "runs.sqlite.zst"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "seed-all"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "runs.sqlite.zst already exists" in completed.stderr
    assert _remote_writes(log.read_text(encoding="utf-8").splitlines()) == []


def test_initial_seed_does_not_treat_head_access_failure_as_absence(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_HEAD_ERROR_KEY"] = "runs.sqlite"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "seed-all"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "AccessDenied" in completed.stderr
    assert _remote_writes(log.read_text(encoding="utf-8").splitlines()) == []


def test_initial_seed_uploads_all_stores_when_contract_keys_are_absent(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "seed-all"],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    commands = log.read_text(encoding="utf-8").splitlines()
    uploads = _compressed_puts(commands)
    assert len(uploads) == 4
    assert all("--key" in command and ".sqlite.zst" in command for command in uploads)
    assert all("--content-type application/zstd --metadata " in command for command in uploads)
    assert all("--if-none-match *" in command for command in uploads)
    # Nothing is being replaced, so no generation is kept.
    assert all(".bak" not in command for command in commands)


def test_initial_seed_uses_conditional_create_when_object_appears_after_preflight(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_CONFLICT_PUT_KEY"] = "market.sqlite.zst"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "seed-all"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    puts = _compressed_puts(log.read_text(encoding="utf-8").splitlines())
    assert len(puts) == 1
    assert "--key market.sqlite.zst" in puts[0]
    assert "--if-none-match *" in puts[0]
    assert not (tmp_path / "aws-state/market.sqlite.zst.object").exists()


def test_initial_seed_ignores_a_kept_generation_of_an_absent_store(tmp_path: Path) -> None:
    # A `.bak` left by an earlier push must not read as the store itself, or a
    # recovery seed after the real key was lost would be refused.
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = "macro.sqlite.bak"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "seed-all"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    commands = log.read_text(encoding="utf-8").splitlines()
    assert len(_compressed_puts(commands)) == 4


def test_machine_store_push_is_github_actions_only(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "outside GitHub Actions" in completed.stderr
    assert not log.exists()


def test_machine_store_push_uploads_three_stores_in_github_actions(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    commands = log.read_text(encoding="utf-8").splitlines()
    assert len(_compressed_puts(commands)) == 3
    assert all(".bak" not in command for command in commands)


def test_second_unchanged_machine_store_push_preserves_the_known_good_backup(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"

    for _ in range(2):
        subprocess.run(
            [TRANSFER_SCRIPT, "push-machine"],
            cwd=REPO_ROOT,
            env=env,
            check=True,
        )

    commands = log.read_text(encoding="utf-8").splitlines()
    uploads = _compressed_puts(commands)
    backups = [command for command in commands if command.startswith("s3api copy-object ")]
    assert len(uploads) == 3
    assert backups == []


def test_machine_store_update_fails_when_conditional_put_loses_a_race(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )
    market_object = tmp_path / "aws-state/market.sqlite.zst.object"
    original = market_object.read_bytes()
    log.unlink()
    env["SQLITE_FAKE_SNAPSHOT_CONTENT"] = "changed"
    env["AWS_FAKE_CONFLICT_PUT_KEY"] = "market.sqlite.zst"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    commands = log.read_text(encoding="utf-8").splitlines()
    put = next(command for command in commands if command.startswith("s3api put-object "))
    assert "--if-match" in put
    assert market_object.read_bytes() == original
    assert (tmp_path / "aws-state/market.sqlite.zst.bak.object").read_bytes() == original


def test_retry_after_ambiguous_commit_does_not_rotate_backup(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    env["AWS_FAKE_FAIL_AFTER_COMMIT_KEY"] = "market.sqlite.zst"

    first = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert first.returncode != 0
    assert second.returncode == 0
    commands = log.read_text(encoding="utf-8").splitlines()
    market_backups = [
        command
        for command in commands
        if command.startswith("s3api copy-object ") and "market.sqlite.zst" in command
    ]
    assert market_backups == []


def test_machine_store_push_finishes_every_snapshot_before_remote_writes(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    env["SQLITE_FAKE_FAIL_BASENAME"] = "macro.sqlite"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert not log.exists()


def test_machine_store_push_keeps_one_generation_of_the_store_it_replaces(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    env["AWS_FAKE_EXISTING_KEY"] = "macro.sqlite"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    commands = log.read_text(encoding="utf-8").splitlines()
    backups = [index for index, command in enumerate(commands) if ".bak" in command]
    uploads = [
        index
        for index, command in enumerate(commands)
        if command.startswith("s3api put-object ") and "--key macro.sqlite.zst" in command
    ]
    assert len(backups) == 1
    # One upload establishes the retained raw object's migration baseline; the other
    # publishes the current snapshot. The baseline is backed up before replacement.
    assert len(uploads) == 2
    # The generation is kept from the compressed object before it is overwritten, through
    # CopyObject: `aws s3 cp` picks its S3-to-S3 implementation by object size and both
    # branches ask R2 for tagging operations it does not implement.
    assert commands[backups[0]].startswith("s3api copy-object ")
    assert "--key macro.sqlite.zst.bak" in commands[backups[0]]
    assert "--copy-source baibai-stores/macro.sqlite.zst" in commands[backups[0]]
    assert "--copy-source-if-match" in commands[backups[0]]
    assert "--if-match" in commands[uploads[1]]
    assert uploads[0] < backups[0] < uploads[1]


def test_store_backup_waits_past_the_cli_default_read_timeout(tmp_path: Path) -> None:
    # R2 answers CopyObject only once the copy is finished, and a several-hundred-MB store
    # outlasts the CLI's 60s default read timeout. Without a wider ceiling the kept
    # generation times out and takes the whole machine-store push down with it.
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    env["AWS_FAKE_EXISTING_KEY"] = "market.sqlite"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    commands = log.read_text(encoding="utf-8").splitlines()
    backup = next(command for command in commands if command.startswith("s3api copy-object "))
    arguments = backup.split()
    assert int(arguments[arguments.index("--cli-read-timeout") + 1]) > 60


def test_macro_push_merges_the_cloud_store_before_uploading(tmp_path: Path) -> None:
    # Runs outside GitHub Actions on purpose: the deep history this publishes is fetched
    # locally, and the merge — not the caller's environment — is what guarantees the cloud
    # store is not rolled back.
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = "macro.sqlite"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-macro"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    commands = log.read_text(encoding="utf-8").splitlines()
    downloads = [
        index
        for index, command in enumerate(commands)
        if command.startswith("s3 cp s3://baibai-stores/macro.sqlite ")
    ]
    merges = [index for index, command in enumerate(commands) if command.startswith("merge ")]
    uploads = [
        index
        for index, command in enumerate(commands)
        if command.startswith("s3api put-object ") and "--key macro.sqlite.zst" in command
    ]
    # The first download feeds the merge. A second verified download establishes
    # the one-time raw/compressed migration baseline before publishing.
    assert len(downloads) == 2
    assert len(merges) == 1
    assert len(uploads) == 2
    assert downloads[0] < merges[0] < downloads[1] < uploads[0]
    assert "--target" in commands[merges[0]]
    assert "data/indicators/macro.sqlite" in commands[merges[0]]
    # Only the indicator store is published; market and runs stay owned by the batch.
    assert all("market.sqlite" not in command for command in commands)
    assert all("runs.sqlite" not in command for command in commands)


def test_macro_push_uploads_nothing_when_the_merge_refuses(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["MERGE_FAKE_EXIT"] = "1"
    env["AWS_FAKE_EXISTING_KEY"] = "macro.sqlite"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-macro"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    commands = log.read_text(encoding="utf-8").splitlines()
    assert _remote_writes(commands) == []


def test_macro_push_fails_if_cloud_raw_changes_after_merge_download(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = "macro.sqlite"
    env["MERGE_FAKE_MUTATE_RAW_HEAD"] = "true"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-macro"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "remote object changed" in completed.stderr
    assert _remote_writes(log.read_text(encoding="utf-8").splitlines()) == []
