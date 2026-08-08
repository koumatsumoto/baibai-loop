from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

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
  if [[ -n "$wants_etag" ]]; then
    # Counts the calls per key so a test can make one object change mid-pull:
    # the version query runs once before the downloads and once after.
    calls="${AWS_FAKE_STATE}/$(printf '%s' "$key" | tr / _)"
    printf 'x' >> "$calls"
    if [[ "$key" == "${AWS_FAKE_CHANGED_KEY:-}" ]]; then
      printf '"etag-%s"\\n' "$(wc -c < "$calls" | tr -d ' ')"
    else
      printf '"etag-stable"\\n'
    fi
    exit 0
  fi
  if [[ -n "${AWS_FAKE_EXISTING_KEY:-}" && "$key" == "$AWS_FAKE_EXISTING_KEY" ]]; then
    printf '{"ContentLength": 1}\\n'
    exit 0
  fi
  printf 'An error occurred (404) when calling the HeadObject operation\\n' >&2
  exit 254
fi
if [[ "$1 $2" == "s3 cp" && "$3" == s3://* ]]; then
  printf 'x' > "$4"
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
script=""
for argument in "$@"; do
  case "${argument}" in
    *sqlite_snapshot.py) script=snapshot ;;
    tools.cloud.merge_indicator_store) script=merge ;;
    tools.cloud.merge_market_store) script=merge ;;
    tools.cloud.migrate_store) script=migrate ;;
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
        printf 'x' > "$1"
      fi
      shift
    done
    exit 0
    ;;
  merge)
    printf 'merge %s\\n' "$*" >> "$AWS_LOG"
    exit "${MERGE_FAKE_EXIT:-0}"
    ;;
  migrate)
    printf 'migrate %s\\n' "$*" >> "$AWS_LOG"
    exit "${MIGRATE_FAKE_EXIT:-0}"
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
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AWS_LOG": str(log),
        "AWS_FAKE_STATE": str(state),
        "R2_ACCOUNT_ID": "account-for-test",
        "R2_ACCESS_KEY_ID": "access-for-test",
        "R2_SECRET_ACCESS_KEY": "secret-for-test",
    }
    environment.pop("GITHUB_ACTIONS", None)
    return environment


def _transfer_commands(log: Path) -> list[str]:
    """The transfers the run performed, without the diagnostic calls.

    The publish path also asks the CLI for its version and for the concurrency it
    resolved, so a real run's log can answer whether the setting took effect. Those
    are not transfers and would otherwise have to be counted in every assertion.
    """
    if not log.exists():
        return []
    return [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if line != "--version" and not line.startswith("configure get ")
    ]


def _fake_repo(tmp_path: Path) -> Path:
    """A repository root the transfer script can write stores into.

    The script resolves every store path from its own location, so a pull run from
    the real checkout writes to the operational stores. Copying `tools/cloud/` into
    a throwaway root lets a pull actually run and be inspected.
    """
    root = tmp_path / "repo"
    (root / "tools/cloud").mkdir(parents=True)
    for name in ("r2_transfer.sh", "sqlite_snapshot.py"):
        shutil.copy(REPO_ROOT / "tools/cloud" / name, root / "tools/cloud" / name)
    (root / "tools/cloud/r2_transfer.sh").chmod(0o755)
    return root


def _serving_export(tmp_path: Path) -> Path:
    output = tmp_path / "serving"
    (output / "views").mkdir(parents=True)
    (output / "views/dashboard.json").write_text("{}", encoding="utf-8")
    (output / "views/meta.json").write_text("{}", encoding="utf-8")
    (output / "history/candidate-views").mkdir(parents=True)
    (output / "history/candidate-views/2026-07-21.json").write_text("{}", encoding="utf-8")
    (output / "history/longlists").mkdir(parents=True)
    (output / "history/longlists/2026-07-21.json").write_text("{}", encoding="utf-8")
    return output


def test_views_upload_replaces_the_mirror_and_leaves_meta_alone(tmp_path: Path) -> None:
    """The stage that runs alongside the store push touches only the mutable image."""
    bin_dir, log = _fake_aws(tmp_path)
    output = _serving_export(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"

    result = subprocess.run(
        [TRANSFER_SCRIPT, "upload-serving-views", output],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    commands = _transfer_commands(log)
    assert len(commands) == 1
    assert commands[0].startswith("s3 sync ")
    assert "s3://baibai-serving/views/" in commands[0]
    assert "--delete --exclude meta.json" in commands[0]
    assert "history/" not in commands[0]
    # One view is mirrored; `meta.json` belongs to the tail stage and is not counted.
    assert "serving views: objects=1 elapsed=" in result.stdout


def test_serving_tail_appends_history_and_writes_meta_last(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    output = _serving_export(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"

    result = subprocess.run(
        [TRANSFER_SCRIPT, "publish-serving-tail", output],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    commands = _transfer_commands(log)
    assert len(commands) == 3
    assert "s3://baibai-serving/history/candidate-views/" in commands[0]
    assert "--delete" not in commands[0]
    assert "s3://baibai-serving/history/longlists/" in commands[1]
    assert "--delete" not in commands[1]
    assert commands[2].startswith("s3 cp ")
    assert commands[2].endswith(
        "s3://baibai-serving/views/meta.json --endpoint-url "
        "https://account-for-test.r2.cloudflarestorage.com --only-show-errors --no-progress"
    )
    assert "serving tail: objects=3 elapsed=" in result.stdout


def test_serving_transfer_raises_concurrency_without_touching_the_caller_config(
    tmp_path: Path,
) -> None:
    """The in-flight count is the only lever on a request-bound upload.

    It has no command-line flag, and this script also runs on a developer machine,
    so writing it into `~/.aws/config` would change transfers this repository has
    nothing to do with.
    """
    bin_dir, log = _fake_aws(tmp_path)
    output = _serving_export(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    env["HOME"] = str(home)

    result = subprocess.run(
        [TRANSFER_SCRIPT, "upload-serving-views", output],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "serving transfer: max_concurrent_requests=32" in result.stdout
    assert not (home / ".aws" / "config").exists()
    # The fake CLI echoes the config the run pointed it at, so the setting is
    # observed where `aws` would read it rather than only where it was written.
    assert "max_concurrent_requests = 32" in result.stdout


def test_serving_concurrency_is_settable_without_a_code_change(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    output = _serving_export(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    env["R2_SERVING_UPLOAD_CONCURRENCY"] = "10"

    result = subprocess.run(
        [TRANSFER_SCRIPT, "upload-serving-views", output],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "serving transfer: max_concurrent_requests=10" in result.stdout


def test_two_concurrent_publishes_do_not_share_a_transfer_config(tmp_path: Path) -> None:
    """Each process writes its own config, so neither can read the other's value.

    The daily batch runs the views mirror beside the store push today, and a fixed
    config path would silently become the last writer's setting the moment the push
    side needed transfer settings too.
    """
    bin_dir, log = _fake_aws(tmp_path)
    output = _serving_export(tmp_path)
    base = _environment(bin_dir, log)
    base["GITHUB_ACTIONS"] = "true"
    # Both processes write into a directory of their own so the survival check below
    # sees these two configs and not whichever ones a parallel test happens to hold
    # open in the shared temporary directory.
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    base["TMPDIR"] = str(scratch)

    processes = []
    for concurrency in ("11", "22"):
        env = {**base, "R2_SERVING_UPLOAD_CONCURRENCY": concurrency, "AWS_LOG": str(log)}
        processes.append(
            subprocess.Popen(
                [TRANSFER_SCRIPT, "upload-serving-views", output],
                cwd=REPO_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        )
    outputs = [process.communicate()[0] for process in processes]

    assert all(process.returncode == 0 for process in processes)
    assert "max_concurrent_requests = 11" in outputs[0]
    assert "max_concurrent_requests = 22" not in outputs[0]
    assert "max_concurrent_requests = 22" in outputs[1]
    assert "max_concurrent_requests = 11" not in outputs[1]
    # The trap removes each config, so neither survives its own process.
    assert not list(scratch.glob("baibai-r2-transfer.*"))


def test_pull_longlist_history_uses_the_dedicated_serving_prefix(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    output = tmp_path / "longlist-history"

    subprocess.run(
        [TRANSFER_SCRIPT, "pull-longlist-history", output],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=True,
    )

    commands = _transfer_commands(log)
    assert len(commands) == 1
    assert commands[0].startswith("s3 sync s3://baibai-serving/history/longlists/")
    assert output.is_dir()


def test_pull_longlist_history_refuses_to_overwrite_a_local_target(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    output = tmp_path / "longlist-history"
    output.mkdir()

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "pull-longlist-history", output],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "refusing longlist history download overwrite" in completed.stderr
    assert not log.exists()


APP_STORE = REPO_ROOT / "data/app/baibai.sqlite"


def test_pull_app_refuses_to_overwrite_a_local_application_store(tmp_path: Path) -> None:
    # The application DB is canonical on the operator's machine: judgments are
    # published locally and only then pushed. Replacing it with the cloud copy
    # destroys anything published since the last push, and nothing can rebuild it.
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    app_store = root / "data/app/baibai.sqlite"
    app_store.parent.mkdir(parents=True)
    app_store.write_bytes(b"local")

    completed = subprocess.run(
        [root / "tools/cloud/r2_transfer.sh", "pull-app"],
        cwd=root,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "refusing application store download overwrite" in completed.stderr
    assert app_store.read_bytes() == b"local"
    assert not log.exists()


def test_only_the_application_pull_names_the_application_store() -> None:
    """No other pull command may reach `baibai.sqlite`.

    The guard inside `pull_app` is the second line of defence; the first is that no
    bulk command lists the application store at all. Asserting the dispatch source
    keeps that structural, because actually running a pull here would write to the
    operational store paths (the script resolves them from its own location).
    """
    script = TRANSFER_SCRIPT.read_text(encoding="utf-8")
    dispatch = script[script.index('case "${1:-}" in') :]
    pulls = {
        line.split(")")[0].strip()
        for line in dispatch.splitlines()
        if line.strip().startswith("pull-") and line.strip().endswith(")")
    }
    naming_app = {
        command
        for command in pulls
        if "baibai.sqlite" in dispatch.split(f"  {command})")[1].split(";;")[0]
        or command == "pull-app"
    }

    assert pulls == {"pull-app", "pull-machine", "pull-market", "pull-longlist-history"}
    assert naming_app == {"pull-app"}


@pytest.mark.parametrize("subcommand", ["upload-serving-views", "publish-serving-tail"])
def test_serving_publish_rejects_an_export_without_meta(tmp_path: Path, subcommand: str) -> None:
    """Both stages refuse a partial export, because the mirror runs with `--delete`."""
    bin_dir, log = _fake_aws(tmp_path)
    output = tmp_path / "serving"
    (output / "views").mkdir(parents=True)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, subcommand, output],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "meta.json is missing" in completed.stderr
    assert not log.exists()


@pytest.mark.parametrize("subcommand", ["upload-serving-views", "publish-serving-tail"])
def test_serving_publish_is_github_actions_only(tmp_path: Path, subcommand: str) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    output = tmp_path / "serving"
    (output / "views").mkdir(parents=True)
    (output / "views/meta.json").write_text("{}", encoding="utf-8")

    completed = subprocess.run(
        [TRANSFER_SCRIPT, subcommand, output],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "outside GitHub Actions" in completed.stderr
    assert not log.exists()


def test_run_summary_upload_writes_one_object_outside_the_views_prefix(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    summary = tmp_path / "workflow-run-summary.json"
    summary.write_text('{"schema_version": 1}', encoding="utf-8")
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"

    subprocess.run(
        [TRANSFER_SCRIPT, "upload-run-summary", summary],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )

    commands = _transfer_commands(log)
    assert len(commands) == 1
    assert commands[0].startswith("s3 cp ")
    # Not under views/, which `upload-serving` mirrors with --delete.
    assert "s3://baibai-serving/system/latest-run.json" in commands[0]


def test_run_summary_upload_reports_a_missing_summary_without_uploading(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "upload-run-summary", tmp_path / "absent.json"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "no workflow run summary to upload" in completed.stderr
    assert not log.exists()


def test_run_summary_upload_is_github_actions_only(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    summary = tmp_path / "workflow-run-summary.json"
    summary.write_text('{"schema_version": 1}', encoding="utf-8")

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "upload-run-summary", summary],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "outside GitHub Actions" in completed.stderr
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
    commands = _transfer_commands(log)
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
    commands = _transfer_commands(log)
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
    commands = _transfer_commands(log)
    assert len([command for command in commands if "s3 cp" in command]) == 3
    assert all(".bak" not in command for command in commands)


def test_machine_store_push_reports_where_each_key_spends_its_time(tmp_path: Path) -> None:
    """A push is three waits and only one of them sends bytes over the link.

    Compressing the snapshot or sending a diff shortens the upload alone. Without the
    split the run's log cannot say whether that is most of the wait or a corner of it,
    and the choice between the two would rest on an inference from throughput.
    """
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"

    result = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    reported = dict(
        re.findall(
            r"store push: key=(\S+) bytes=(\d+) snapshot=\d+s backup=\d+s upload=\d+s",
            result.stdout,
        )
    )
    # The staged snapshot is what gets uploaded, so its size is the transferred byte
    # count. The fake snapshot writes one byte.
    assert reported == {"market.sqlite": "1", "runs.sqlite": "1", "macro.sqlite": "1"}


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
    commands = _transfer_commands(log)
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
    commands = _transfer_commands(log)
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
    commands = _transfer_commands(log)
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
    migrations = [index for index, command in enumerate(commands) if command.startswith("migrate ")]
    assert len(downloads) == 1
    assert len(merges) == 1
    assert len(uploads) == 1
    assert len(migrations) == 1
    assert downloads[0] < migrations[0] < merges[0] < uploads[0]
    assert "--store macro" in commands[migrations[0]]
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
    commands = _transfer_commands(log)
    assert all(
        not command.rstrip().endswith(
            "s3://baibai-stores/macro.sqlite --endpoint-url "
            "https://account-for-test.r2.cloudflarestorage.com --only-show-errors --no-progress"
        )
        for command in commands
    )


def test_market_push_merges_the_cloud_store_before_uploading(tmp_path: Path) -> None:
    # Runs outside GitHub Actions on purpose. The deep history this publishes is fetched
    # where there is time for it, and the merge — not the caller's environment — is what
    # keeps the daily batch's recent rows from being rolled back.
    bin_dir, log = _fake_aws(tmp_path)

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-market"],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    commands = _transfer_commands(log)
    downloads = [
        index
        for index, command in enumerate(commands)
        if command.startswith("s3 cp s3://baibai-stores/market.sqlite ")
    ]
    merges = [index for index, command in enumerate(commands) if command.startswith("merge ")]
    uploads = [
        index
        for index, command in enumerate(commands)
        if command.rstrip().endswith(
            "s3://baibai-stores/market.sqlite --endpoint-url "
            "https://account-for-test.r2.cloudflarestorage.com --only-show-errors --no-progress"
        )
    ]
    migrations = [index for index, command in enumerate(commands) if command.startswith("migrate ")]
    assert len(downloads) == 1
    assert len(merges) == 1
    assert len(uploads) == 1
    # The merge requires source and target on the same schema, and the source is whatever
    # R2 holds. Without this step a store published before a migration landed could only
    # be moved forward by the daily batch, so every schema change would block publishing
    # from a developer machine until the cloud had run.
    assert len(migrations) == 1
    assert downloads[0] < migrations[0] < merges[0] < uploads[0]
    assert "--store market" in commands[migrations[0]]
    assert "data/screening/market.sqlite" in commands[merges[0]]
    # Only the market store is published; runs and the indicator store are untouched.
    assert all("runs.sqlite" not in command for command in commands)
    assert all("macro.sqlite" not in command for command in commands)


def test_market_push_uploads_nothing_when_the_merge_refuses(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["MERGE_FAKE_EXIT"] = "1"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-market"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    commands = _transfer_commands(log)
    assert all(
        not command.rstrip().endswith(
            "s3://baibai-stores/market.sqlite --endpoint-url "
            "https://account-for-test.r2.cloudflarestorage.com --only-show-errors --no-progress"
        )
        for command in commands
    )


def _machine_stores(root: Path) -> dict[str, Path]:
    return {
        "market.sqlite": root / "data/screening/market.sqlite",
        "runs.sqlite": root / "data/screening/runs.sqlite",
        "macro.sqlite": root / "data/indicators/macro.sqlite",
    }


def test_pull_machine_refuses_a_snapshot_that_straddles_a_push(tmp_path: Path) -> None:
    """A batch that pushes partway through the pull would leave mixed generations.

    Each store passes `check_sqlite` on its own, so nothing downstream notices that
    the run store knows a screening run the market store has no bars for.
    """

    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    for path in _machine_stores(root).values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"local")
    environment = _environment(bin_dir, log)
    environment["AWS_FAKE_CHANGED_KEY"] = "runs.sqlite"

    completed = subprocess.run(
        [root / "tools/cloud/r2_transfer.sh", "pull-machine"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "runs.sqlite changed on R2 during the pull" in completed.stderr
    # No store is replaced, so the local set stays one consistent generation.
    assert all(path.read_bytes() == b"local" for path in _machine_stores(root).values())
    assert not list(root.glob(".r2-transfer.*"))
    commands = _transfer_commands(log)
    versions = [index for index, line in enumerate(commands) if "--query ETag" in line]
    downloads = [index for index, line in enumerate(commands) if line.startswith("s3 cp s3://")]
    # Every store's version is read before any download and re-read after all of
    # them; a straddle is only visible from both sides.
    assert len(downloads) == 3
    assert versions[:3] == [0, 1, 2]
    assert min(versions[3:]) > max(downloads)


def test_pull_machine_replaces_every_store_when_no_push_intervened(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    for path in _machine_stores(root).values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"local")

    completed = subprocess.run(
        [root / "tools/cloud/r2_transfer.sh", "pull-machine"],
        cwd=root,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert all(path.read_bytes() == b"x" for path in _machine_stores(root).values())
    assert not list(root.glob(".r2-transfer.*"))
