from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSFER_SCRIPT = REPO_ROOT / "tools/cloud/r2_transfer.sh"


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
  if [[ -n "${AWS_FAKE_EXISTING_KEY:-}" && "$key" == "$AWS_FAKE_EXISTING_KEY" ]]; then
    printf '{"ContentLength": 1}\\n'
    exit 0
  fi
  printf 'An error occurred (404) when calling the HeadObject operation\\n' >&2
  exit 254
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
    *merge_indicator_store.py) script=merge ;;
  esac
done
case "${script}" in
  snapshot)
    while [[ $# -gt 0 ]]; do
      if [[ "$1" == "--output" ]]; then
        shift
        : > "$1"
      fi
      shift
    done
    exit 0
    ;;
  merge)
    printf 'merge %s\\n' "$*" >> "$AWS_LOG"
    exit "${MERGE_FAKE_EXIT:-0}"
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
        "R2_ACCOUNT_ID": "account-for-test",
        "R2_ACCESS_KEY_ID": "access-for-test",
        "R2_SECRET_ACCESS_KEY": "secret-for-test",
    }
    environment.pop("GITHUB_ACTIONS", None)
    return environment


def test_upload_serving_replaces_views_appends_history_and_writes_meta_last(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    output = tmp_path / "serving"
    (output / "views").mkdir(parents=True)
    (output / "views/dashboard.json").write_text("{}", encoding="utf-8")
    (output / "views/meta.json").write_text("{}", encoding="utf-8")
    (output / "history/select").mkdir(parents=True)
    (output / "history/select/2026-07-21.json").write_text("{}", encoding="utf-8")
    (output / "history/candidate-views").mkdir(parents=True)
    (output / "history/candidate-views/2026-07-21.json").write_text("{}", encoding="utf-8")

    subprocess.run(
        [TRANSFER_SCRIPT, "upload-serving", output],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=True,
    )

    commands = log.read_text(encoding="utf-8").splitlines()
    assert len(commands) == 4
    assert commands[0].startswith("s3 sync ")
    assert "s3://baibai-serving/views/" in commands[0]
    assert "--delete --exclude meta.json" in commands[0]
    assert "s3://baibai-serving/history/select/" in commands[1]
    assert "s3://baibai-serving/history/candidate-views/" in commands[2]
    assert "--delete" not in commands[1]
    assert "--delete" not in commands[2]
    assert commands[3].startswith("s3 cp ")
    assert commands[3].endswith(
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
    assert all("s3 cp" not in command for command in log.read_text(encoding="utf-8").splitlines())


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
    assert len([command for command in commands if "s3 cp" in command]) == 4
    # Nothing is being replaced, so no generation is kept.
    assert all(".bak" not in command for command in commands)


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
    assert len([command for command in commands if "s3 cp" in command]) == 4


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
    assert len([command for command in commands if "s3 cp" in command]) == 3
    assert all(".bak" not in command for command in commands)


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
        if "s3 cp" in command
        and command.endswith(
            "s3://baibai-stores/macro.sqlite --endpoint-url "
            "https://account-for-test.r2.cloudflarestorage.com --only-show-errors --no-progress"
        )
    ]
    assert len(backups) == 1
    assert len(uploads) == 1
    # The generation is kept from the remote object before it is overwritten, through
    # CopyObject: `aws s3 cp` picks its S3-to-S3 implementation by object size and both
    # branches ask R2 for tagging operations it does not implement.
    assert commands[backups[0]].startswith("s3api copy-object ")
    assert "--key macro.sqlite.bak" in commands[backups[0]]
    assert "--copy-source baibai-stores/macro.sqlite" in commands[backups[0]]
    assert backups[0] < uploads[0]


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

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-macro"],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
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
        if command.rstrip().endswith(
            "s3://baibai-stores/macro.sqlite --endpoint-url "
            "https://account-for-test.r2.cloudflarestorage.com --only-show-errors --no-progress"
        )
    ]
    assert len(downloads) == 1
    assert len(merges) == 1
    assert len(uploads) == 1
    assert downloads[0] < merges[0] < uploads[0]
    assert "--target" in commands[merges[0]]
    assert "data/indicators/macro.sqlite" in commands[merges[0]]
    # Only the indicator store is published; market and runs stay owned by the batch.
    assert all("market.sqlite" not in command for command in commands)
    assert all("runs.sqlite" not in command for command in commands)


def test_macro_push_uploads_nothing_when_the_merge_refuses(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["MERGE_FAKE_EXIT"] = "1"

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
    assert all(
        not command.rstrip().endswith(
            "s3://baibai-stores/macro.sqlite --endpoint-url "
            "https://account-for-test.r2.cloudflarestorage.com --only-show-errors --no-progress"
        )
        for command in commands
    )
