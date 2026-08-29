from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from baibai_engine.market.sqlite import open_connection
from baibai_engine.market.sqlite.lake_origin import LakeStoreOrigin, write_lake_store_origin

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSFER_SCRIPT = REPO_ROOT / "batch/scripts/r2_transfer.sh"


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
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--key" ]]; then
      key="$2"
    fi
    if [[ "$1" == "--body" ]]; then
      body="$2"
    fi
    shift
  done
  # Every uploaded body is kept so a test can read what was actually published
  # rather than only that a call was made.
  if [[ -n "$body" && -f "$body" ]]; then
    cp "$body" "${AWS_FAKE_STATE}/put-$(printf '%s' "$key" | tr / _)"
  fi
  if [[ -n "${AWS_FAKE_FAIL_PUT_KEY:-}" && "$key" == "${AWS_FAKE_FAIL_PUT_KEY}" ]]; then
    printf 'An error occurred (InternalError) when calling PutObject\n' >&2
    exit 253
  fi
  slug="$(printf '%s' "$key" | tr / _)"
  printf 'x' >> "${AWS_FAKE_STATE}/generation-${slug}"
  rm -f "${AWS_FAKE_STATE}/deleted-${slug}"
  exit 0
fi
if [[ "$1 $2" == "s3 cp" && "$3" == s3://* ]]; then
  printf 'x' > "$4"
fi
if [[ "$1 $2" == "s3 cp" && "$4" == s3://* ]]; then
  key="${4#s3://}"
  key="${key#*/}"
  slug="$(printf '%s' "$key" | tr / _)"
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
if [[ "$*" == *"baibai-engine lake dehydrate"* ]]; then
  printf 'dehydrate %s\\n' "$*" >> "$AWS_LOG"
  exit 0
fi
if [[ "$*" == *"baibai-engine lake hydrate"* ]]; then
  printf 'hydrate %s\\n' "$*" >> "$AWS_LOG"
  exit 0
fi
script=""
for argument in "$@"; do
  case "${argument}" in
    baibai_batch.storage.sqlite_snapshot) script=snapshot ;;
    baibai_batch.storage.merge_indicator_store) script=merge ;;
    baibai_batch.storage.merge_market_store) script=merge ;;
    baibai_batch.storage.migrate_store) script=migrate ;;
    tools/migrations/cutover_market_v25.py) script=cutover ;;
    tools/migrations/cutover_runs_v4.py) script=run_cutover ;;
    baibai_batch.validation.repository_layout) script=layout ;;
    baibai_batch.storage.publish_market_lake) script=publish ;;
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
    if [[ "$*" == *" version "* ]]; then
      printf '%s\\n' "${SQLITE_FAKE_VERSION:-13}"
      exit 0
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
  migrate)
    printf 'migrate %s\\n' "$*" >> "$AWS_LOG"
    exit "${MIGRATE_FAKE_EXIT:-0}"
    ;;
  cutover)
    printf 'cutover %s\\n' "$*" >> "$AWS_LOG"
    exit "${CUTOVER_FAKE_EXIT:-0}"
    ;;
  run_cutover)
    printf 'run-cutover %s\\n' "$*" >> "$AWS_LOG"
    exit "${RUN_CUTOVER_FAKE_EXIT:-0}"
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
    for key in ("market.sqlite", "runs.sqlite", "macro.sqlite"):
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


def test_transfer_rejects_a_split_layout_before_loading_credentials(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    environment = _environment(bin_dir, log)
    environment["LAYOUT_FAKE_EXIT"] = "2"
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        environment.pop(name)

    completed = subprocess.run(
        [root / "batch/scripts/r2_transfer.sh", "pull-machine"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert _transfer_commands(log) == []


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
    the real checkout writes to the operational stores. Copying `batch/scripts/` into
    a throwaway root lets a pull actually run and be inspected.
    """
    root = tmp_path / "repo"
    (root / "batch/scripts").mkdir(parents=True)
    shutil.copy(TRANSFER_SCRIPT, root / "batch/scripts/r2_transfer.sh")
    (root / "batch/scripts/r2_transfer.sh").chmod(0o755)
    return root


def test_sqlite_helpers_run_from_the_repository_when_called_elsewhere(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    environment = _environment(bin_dir, log)
    environment["UV_EXPECTED_CWD"] = str(root)

    completed = subprocess.run(
        [root / "batch/scripts/r2_transfer.sh", "pull-machine"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_hydrate_cuts_over_a_pulled_store_before_writing_the_origin(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    environment = _environment(bin_dir, log)

    completed = subprocess.run(
        [root / "batch/scripts/r2_transfer.sh", "hydrate-market"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    commands = log.read_text(encoding="utf-8").splitlines()
    cutover = next(
        index for index, command in enumerate(commands) if command.startswith("cutover ")
    )
    hydration = next(
        index for index, command in enumerate(commands) if command.startswith("hydrate ")
    )
    assert cutover < hydration
    assert "cutover_market_v25.py" in commands[cutover]


def _serving_export(tmp_path: Path) -> Path:
    output = tmp_path / "serving"
    (output / "views").mkdir(parents=True)
    (output / "views/dashboard.json").write_text("{}", encoding="utf-8")
    (output / "views/meta.json").write_text("{}", encoding="utf-8")
    (output / "history/candidate-views").mkdir(parents=True)
    (output / "history/candidate-views/2026-07-21.json").write_text("{}", encoding="utf-8")
    (output / "history/ranked_sets").mkdir(parents=True)
    (output / "history/ranked_sets/2026-07-21.json").write_text("{}", encoding="utf-8")
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
    assert "s3://baibai-serving/history/ranked_sets/" in commands[1]
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


def test_pull_ranked_set_history_uses_the_dedicated_serving_prefix(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    output = tmp_path / "ranked-set-history"

    subprocess.run(
        [TRANSFER_SCRIPT, "pull-ranked-set-history", output],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=True,
    )

    commands = _transfer_commands(log)
    assert len(commands) == 1
    assert commands[0].startswith("s3 sync s3://baibai-serving/history/ranked_sets/")
    assert output.is_dir()


def test_pull_ranked_set_history_refuses_to_overwrite_a_local_target(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    output = tmp_path / "ranked-set-history"
    output.mkdir()

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "pull-ranked-set-history", output],
        cwd=REPO_ROOT,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "refusing ranked-set history download overwrite" in completed.stderr
    assert not log.exists()


APP_STORE = REPO_ROOT / "stores/application/baibai.sqlite"


def test_pull_app_refuses_to_overwrite_a_local_application_store(tmp_path: Path) -> None:
    # The application DB is canonical on the operator's machine: judgments are
    # published locally and only then pushed. Replacing it with the cloud copy
    # destroys anything published since the last push, and nothing can rebuild it.
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    app_store = root / "stores/application/baibai.sqlite"
    app_store.parent.mkdir(parents=True)
    app_store.write_bytes(b"local")

    completed = subprocess.run(
        [root / "batch/scripts/r2_transfer.sh", "pull-app"],
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

    assert pulls == {
        "pull-app",
        "pull-machine",
        "pull-market",
        "pull-runs",
        "pull-ranked-set-history",
    }
    assert naming_app == {"pull-app"}


def test_pull_runs_is_scoped_to_the_cloud_authoritative_run_store() -> None:
    script = TRANSFER_SCRIPT.read_text(encoding="utf-8")
    dispatch = script[script.index('case "${1:-}" in') :]
    block = dispatch.split("  pull-runs)")[1].split(";;")[0]

    assert "pull_keys runs.sqlite" in block
    assert "market.sqlite" not in block
    assert "macro.sqlite" not in block


def test_one_time_run_cutover_is_conditional_and_scoped_to_runs(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    environment = _environment(bin_dir, log)
    environment["AWS_FAKE_EXISTING_KEY"] = "runs.sqlite"

    completed = subprocess.run(
        [root / "batch/scripts/r2_transfer.sh", "cutover-runs"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    commands = _transfer_commands(log)
    cutover_index = next(
        index for index, command in enumerate(commands) if command.startswith("run-cutover ")
    )
    upload_index = next(
        index
        for index, command in enumerate(commands)
        if command.startswith("s3api put-object ") and "--key runs.sqlite" in command
    )
    assert cutover_index < upload_index
    assert '--if-match "etag-stable"' in commands[upload_index]
    assert not any(
        "--key market.sqlite" in command or "--key macro.sqlite" in command
        for command in commands
        if command.startswith("s3api put-object ")
    )
    assert "--key machine-manifest.json" in commands[-1]


def test_one_time_run_cutover_refuses_remote_drift_before_local_replacement(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    environment = _environment(bin_dir, log)
    environment["AWS_FAKE_CHANGED_KEY"] = "runs.sqlite"

    completed = subprocess.run(
        [root / "batch/scripts/r2_transfer.sh", "cutover-runs"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "changed on R2 after the pull" in completed.stderr
    assert not any(command.startswith("run-cutover ") for command in _transfer_commands(log))


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
    uploads = [command for command in commands if command.startswith("s3api put-object ")]
    stores = [command for command in uploads if "--key machine-manifest.json" not in command]
    assert len(stores) == 3
    assert all('--if-match "etag-stable"' in command for command in stores)
    assert all(".bak" not in command for command in commands)
    # The receipt is written last and unconditionally: it describes the set the three
    # pushes just produced, so it cannot be bound to a generation any of them replaced.
    assert uploads[-1] == uploads[3]
    assert "--key machine-manifest.json" in uploads[-1]
    assert "--if-match" not in uploads[-1]


def _receipt(state: Path) -> str | None:
    written = state / "put-machine-manifest.json"
    return written.read_text(encoding="utf-8") if written.exists() else None


def _pull_machine(tmp_path: Path, *, receipt: str | None) -> subprocess.CompletedProcess[str]:
    """Run a bundle pull in a throwaway root: the script resolves store paths from its
    own location, so running it from the checkout replaces the operational stores."""

    pull_root = tmp_path / "pull"
    pull_root.mkdir(exist_ok=True)
    bin_dir, log = _fake_aws(pull_root)
    root = _fake_repo(pull_root)
    env = _environment(bin_dir, log)
    if receipt is not None:
        env["AWS_FAKE_EXISTING_KEY"] = "machine-manifest.json"
        env["AWS_FAKE_OBJECT_KEY"] = "machine-manifest.json"
        env["AWS_FAKE_OBJECT_BODY"] = receipt
    return subprocess.run(
        [root / "batch/scripts/r2_transfer.sh", "pull-machine"],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_a_push_that_stops_partway_leaves_no_receipt_for_the_set_it_made(
    tmp_path: Path,
) -> None:
    """Three conditional PUTs cannot be one commit, so the second one failing leaves the
    bucket holding a set no batch produced. The receipt is written only after all three,
    so what survives is the previous set's receipt — which is what the next pull reads."""

    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    env["AWS_FAKE_FAIL_PUT_KEY"] = "runs.sqlite"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    commands = _transfer_commands(log)
    pushed = [
        command
        for command in commands
        if command.startswith("s3api put-object ") and "--key machine-manifest.json" not in command
    ]
    # The market store went out; runs failed; macro was never attempted.
    assert len(pushed) == 2
    assert "--key market.sqlite" in pushed[0]
    assert "--key runs.sqlite" in pushed[1]
    assert not any("--key macro.sqlite" in command for command in pushed)
    assert _receipt(Path(env["AWS_FAKE_STATE"])) is None


def test_a_bundle_pull_refuses_a_set_the_receipt_does_not_name(tmp_path: Path) -> None:
    """Per key the mixed set looks intact — the straddle check compares each object with
    itself — so only the receipt can tell the pull that these three were never together."""

    stale = (
        '{\n  "schema_version": 1,\n  "written_at_utc": "2026-08-18T08:37:45Z",\n'
        '  "keys": {\n'
        '    "market.sqlite": "etag-before-the-failed-push",\n'
        '    "runs.sqlite": "etag-stable",\n'
        '    "macro.sqlite": "etag-stable"\n  }\n}\n'
    )

    completed = _pull_machine(tmp_path, receipt=stale)

    assert completed.returncode == 1
    assert "R2 holds market.sqlite at generation etag-stable" in completed.stderr
    assert "receipt names etag-before-the-failed-push" in completed.stderr
    assert "wait for it to finish and pull again" in completed.stderr
    # Not "retry": a push that stopped partway is not cleared by running it again.
    assert "no retry clears this by itself" in completed.stderr
    assert "batch/OPERATIONS.md" in completed.stderr
    # The refusal happens before anything is downloaded, so the local stores stand.
    assert not any(
        command.startswith("s3 cp s3://baibai-stores/")
        for command in _transfer_commands(tmp_path / "pull" / "aws.log")
    )


def test_a_bundle_pull_accepts_the_set_the_receipt_names(tmp_path: Path) -> None:
    current = (
        '{\n  "schema_version": 1,\n  "written_at_utc": "2026-08-19T00:00:00Z",\n'
        '  "keys": {\n'
        '    "market.sqlite": "etag-stable",\n'
        '    "runs.sqlite": "etag-stable",\n'
        '    "macro.sqlite": "etag-stable"\n  }\n}\n'
    )

    completed = _pull_machine(tmp_path, receipt=current)

    assert completed.returncode == 0, completed.stderr
    assert "machine bundle receipt matches" in completed.stdout


def test_a_receipt_that_does_not_read_back_refuses_rather_than_passing(tmp_path: Path) -> None:
    """A receipt this script cannot parse says nothing about the set, and nothing is
    exactly what a check that waves it through would also say."""

    completed = _pull_machine(tmp_path, receipt='{"keys": {"market.sqlite": ""}}\n')

    assert completed.returncode == 1
    assert "receipt names no generation" in completed.stderr


RECOVERY_SECTION = "部分 push からの復旧"


def test_the_rejection_names_a_recovery_the_runbook_actually_carries() -> None:
    """The message is the only thing an operator has at the moment the pull stops.

    It used to say "re-dispatch the batch", which cannot work: the re-dispatch starts
    with the same pull and stops at the same place. Pointing at a section is only better
    while the section exists and carries the step it promises.
    """

    message = (REPO_ROOT / "batch/scripts/r2_transfer.sh").read_text(encoding="utf-8")
    runbook = (REPO_ROOT / "batch/OPERATIONS.md").read_text(encoding="utf-8")

    assert RECOVERY_SECTION in message
    assert "batch/OPERATIONS.md" in message
    assert "re-dispatch" not in message
    assert f"### {RECOVERY_SECTION}" in runbook
    # The step the message sends the reader to: one exact key, never a prefix.
    assert "--key machine-manifest.json" in runbook


def test_a_retry_after_a_partial_push_is_refused_by_its_own_precheck(tmp_path: Path) -> None:
    """Recovery is not "run it again".

    Each conditional PUT is bound to the generation the failed run pulled, and the one
    that already succeeded changed that key. Running the command again — by hand or by
    re-dispatching the batch — is refused before it writes anything.
    """

    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"

    first = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env={**env, "AWS_FAKE_FAIL_PUT_KEY": "runs.sqlite"},
        check=False,
        capture_output=True,
        text=True,
    )
    log.write_text("", encoding="utf-8")
    retry = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert first.returncode != 0
    assert retry.returncode != 0
    assert "market.sqlite changed on R2 after the pull" in retry.stderr
    # Refused before writing: the mixed set the first run left is not made worse.
    assert not [
        command
        for command in _transfer_commands(log)
        if command.startswith(("s3api put-object ", "s3api copy-object "))
    ]


def test_the_documented_recovery_puts_a_partially_pushed_bucket_back_in_step(
    tmp_path: Path,
) -> None:
    """`batch/OPERATIONS.md` step (i), end to end: delete the receipt, take the set
    unverified once, and let the next complete push write the receipt again.

    Every stage runs the real script against the fake bucket, so what is being checked
    is the procedure rather than a description of it.
    """

    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    env = _environment(bin_dir, log)
    state = Path(env["AWS_FAKE_STATE"])
    script = root / "batch/scripts/r2_transfer.sh"

    def run(command: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [script, command],
            cwd=root,
            env={**env, **overrides},
            check=False,
            capture_output=True,
            text=True,
        )

    # A complete push, then the pull that records what it wrote: the steady state.
    assert run("push-machine", GITHUB_ACTIONS="true").returncode == 0
    settled_pull = run("pull-machine")
    assert settled_pull.returncode == 0
    # The accepting line, not just the exit code: a pull that found no receipt also
    # exits 0, so the code alone cannot tell verified from unverified.
    assert "machine bundle receipt matches" in settled_pull.stdout
    settled = _receipt(state)
    assert settled is not None
    assert '"market.sqlite": "etag-put-1"' in settled

    # The failure this recovers from: the second key's PUT does not land.
    partial = run("push-machine", GITHUB_ACTIONS="true", AWS_FAKE_FAIL_PUT_KEY="runs.sqlite")
    assert partial.returncode != 0
    # The receipt still names the previous set, which is now not what the bucket holds.
    assert _receipt(state) == settled

    stuck = run("pull-machine")
    assert stuck.returncode == 1
    assert "R2 holds market.sqlite at generation etag-put-2" in stuck.stderr

    # Step (i): one exact key, the same call the runbook prints.
    removed = subprocess.run(
        [
            "aws",
            "s3api",
            "delete-object",
            "--bucket",
            "baibai-stores",
            "--key",
            "machine-manifest.json",
            "--endpoint-url",
            "https://account-for-test.r2.cloudflarestorage.com",
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert removed.returncode == 0
    assert _receipt(state) is None

    unverified = run("pull-machine")
    assert unverified.returncode == 0
    assert "taking the store set unverified" in unverified.stderr

    assert run("push-machine", GITHUB_ACTIONS="true").returncode == 0
    rewritten = _receipt(state)
    assert rewritten is not None
    # The generations moved: the recovery published a set, it did not restate the old one.
    assert '"market.sqlite": "etag-put-3"' in rewritten
    assert '"runs.sqlite": "etag-put-2"' in rewritten
    assert '"macro.sqlite": "etag-put-2"' in rewritten

    # And the receipt the recovery wrote is one a later pull verifies against.
    verified = run("pull-machine")
    assert verified.returncode == 0
    assert "machine bundle receipt matches" in verified.stdout


def test_machine_store_push_rejects_a_generation_replaced_by_manual_publish(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    env["AWS_FAKE_ETAG_OVERRIDE"] = '"etag-after-manual-publish"'

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "market.sqlite changed on R2 after the pull" in completed.stderr
    commands = _transfer_commands(log)
    assert not any(command.startswith("s3api copy-object ") for command in commands)
    assert not any(command.startswith("s3api put-object ") for command in commands)


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


def _push_app(
    tmp_path: Path, *, listed: str | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = "baibai.sqlite"
    if listed is not None:
        env["AWS_FAKE_LIST_KEYS"] = listed
    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-app"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed, _transfer_commands(log)


def test_application_store_push_keeps_a_dated_generation(tmp_path: Path) -> None:
    """The judgments and the ledger are the one thing here nothing regenerates, and a
    single `.bak` is a single undo: a damaged store pushed twice has already spent it."""

    completed, commands = _push_app(tmp_path)
    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d")

    assert completed.returncode == 0, completed.stderr
    backups = [command for command in commands if command.startswith("s3api copy-object ")]
    assert len(backups) == 1
    assert f"--key baibai.sqlite.bak-{today}" in backups[0]
    # Same-day pushes land on the same key, so the kept set is one generation per day.
    assert "--copy-source baibai-stores/baibai.sqlite" in backups[0]


def test_application_store_push_prunes_beyond_the_kept_generations(tmp_path: Path) -> None:
    listed = "\t".join(f"baibai.sqlite.bak-202608{day:02d}" for day in range(1, 17))

    completed, commands = _push_app(tmp_path, listed=listed)
    deletes = [command for command in commands if command.startswith("s3api delete-object ")]

    assert completed.returncode == 0, completed.stderr
    # Sixteen generations, fourteen kept: the two oldest go, named exactly.
    assert [command.split("--key ")[1].split(" ")[0] for command in deletes] == [
        "baibai.sqlite.bak-20260802",
        "baibai.sqlite.bak-20260801",
    ]


def test_application_store_prune_leaves_keys_it_did_not_write(tmp_path: Path) -> None:
    """A prefix delete would take whatever else sits under the prefix. The pre-dated
    single `.bak` is exactly that, and it is still a generation worth having."""

    listed = "\t".join(
        [
            "baibai.sqlite.bak",
            "baibai.sqlite.bak-notadate",
            *(f"baibai.sqlite.bak-202608{day:02d}" for day in range(1, 17)),
        ]
    )

    completed, commands = _push_app(tmp_path, listed=listed)
    deleted = [
        command.split("--key ")[1].split(" ")[0]
        for command in commands
        if command.startswith("s3api delete-object ")
    ]

    assert completed.returncode == 0, completed.stderr
    assert deleted == ["baibai.sqlite.bak-20260802", "baibai.sqlite.bak-20260801"]
    assert "leaving an unrecognised key" in completed.stderr


def test_machine_store_backups_stay_a_single_generation(tmp_path: Path) -> None:
    """The three machine stores are rewritten every business day and rebuildable from
    their sources, so a generation each would only hold copies of a store the next
    batch replaces anyway."""

    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["GITHUB_ACTIONS"] = "true"
    env["AWS_FAKE_EXISTING_KEY"] = "macro.sqlite"

    subprocess.run(
        [TRANSFER_SCRIPT, "push-machine"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    backups = [
        command for command in _transfer_commands(log) if command.startswith("s3api copy-object ")
    ]

    assert len(backups) == 1
    assert "--key macro.sqlite.bak " in f"{backups[0]} "


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
        if command.startswith("s3api put-object ") and "--key macro.sqlite" in command
    ]
    assert len(backups) == 1
    assert len(uploads) == 1
    # The generation is kept from the remote object before it is overwritten, through
    # CopyObject: `aws s3 cp` picks its S3-to-S3 implementation by object size and both
    # branches ask R2 for tagging operations it does not implement.
    assert commands[backups[0]].startswith("s3api copy-object ")
    assert "--key macro.sqlite.bak" in commands[backups[0]]
    assert "--copy-source baibai-stores/macro.sqlite" in commands[backups[0]]
    assert '--copy-source-if-match "etag-stable"' in commands[backups[0]]
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
        if command.startswith("s3api put-object ") and "--key macro.sqlite" in command
    ]
    migrations = [index for index, command in enumerate(commands) if command.startswith("migrate ")]
    assert len(downloads) == 1
    assert len(merges) == 1
    assert len(uploads) == 1
    assert len(migrations) == 1
    assert downloads[0] < migrations[0] < merges[0] < uploads[0]
    assert "--store macro" in commands[migrations[0]]
    assert "--target" in commands[merges[0]]
    assert "stores/macro/macro.sqlite" in commands[merges[0]]
    assert '--if-match "etag-stable"' in commands[uploads[0]]
    # Only the indicator store is published; market and runs stay owned by the batch.
    # The receipt that follows reads their generations back, so what has to stay absent
    # is a write of them, not a mention.
    writes = [
        command
        for command in commands
        if command.startswith(("s3api put-object ", "s3api copy-object ", "s3 cp "))
    ]
    assert all("market.sqlite" not in command for command in writes)
    assert all("runs.sqlite" not in command for command in writes)


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
    assert not any(
        command.startswith("s3api put-object ") and "--key macro.sqlite" in command
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
        if command.startswith("s3api put-object ") and "--key market.sqlite" in command
    ]
    cutovers = [index for index, command in enumerate(commands) if command.startswith("cutover ")]
    assert len(downloads) == 1
    assert len(merges) == 1
    assert len(uploads) == 1
    # The merge requires source and target on the same schema, and the source is whatever
    # R2 holds. Without this step a store published before a migration landed could only
    # be moved forward by the daily batch, so every schema change would block publishing
    # from a developer machine until the cloud had run.
    assert len(cutovers) == 1
    assert downloads[0] < cutovers[0] < merges[0] < uploads[0]
    assert "cutover_market_v25.py" in commands[cutovers[0]]
    assert "stores/market/market.sqlite" in commands[merges[0]]
    assert '--if-match "etag-stable"' in commands[uploads[0]]
    # Only the market store is published. The receipt that follows reads the other two
    # generations back, so what has to stay absent is a write of them, not a mention.
    writes = [
        command
        for command in commands
        if command.startswith(("s3api put-object ", "s3api copy-object ", "s3 cp "))
    ]
    assert all("runs.sqlite" not in command for command in writes)
    assert all("macro.sqlite" not in command for command in writes)


def test_market_dehydrate_resolves_current_and_leaves_identity_check_to_the_core(
    tmp_path: Path,
) -> None:
    root = _fake_repo(tmp_path)
    market = root / "stores/market/market.sqlite"
    market.parent.mkdir(parents=True)
    connection = open_connection(market)
    write_lake_store_origin(
        connection,
        LakeStoreOrigin(
            release_id="sqlite-release",
            release_manifest_sha256="c" * 64,
        ),
    )
    connection.commit()
    connection.close()
    bin_dir, log = _fake_aws(tmp_path)
    environment = _environment(bin_dir, log)
    environment["SNAPSHOT_FAKE_COPY"] = "1"
    environment["AWS_FAKE_EXISTING_KEY"] = "lake/pointers/l1/current.json"

    completed = subprocess.run(
        [root / "batch/scripts/r2_transfer.sh", "push-market"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    dehydrate = next(
        command
        for command in log.read_text(encoding="utf-8").splitlines()
        if command.startswith("dehydrate ")
    )
    assert "--release" not in dehydrate
    assert "--manifest-sha256" not in dehydrate


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
    assert not any(
        command.startswith("s3api put-object ") and "--key market.sqlite" in command
        for command in commands
    )


def test_market_push_stops_before_backup_when_cloud_changes_during_merge(tmp_path: Path) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_CHANGED_KEY"] = "market.sqlite"
    env["AWS_FAKE_EXISTING_KEY"] = "market.sqlite"

    completed = subprocess.run(
        [TRANSFER_SCRIPT, "push-market"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "market.sqlite changed on R2 during the merge" in completed.stderr
    commands = _transfer_commands(log)
    assert not any(command.startswith("s3api copy-object ") for command in commands)
    assert not any(command.startswith("s3api put-object ") for command in commands)


def test_market_push_condition_rejects_a_race_after_the_last_version_read(
    tmp_path: Path,
) -> None:
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_REJECT_CONDITIONAL_PUT"] = "1"

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
    upload = next(command for command in commands if command.startswith("s3api put-object "))
    assert "--key market.sqlite" in upload
    assert '--if-match "etag-stable"' in upload
    assert not any(
        command.startswith("s3 cp ") and "s3://baibai-stores/market.sqlite" in command
        for command in commands
        if not command.startswith("s3 cp s3://")
    )


def _machine_stores(root: Path) -> dict[str, Path]:
    return {
        "market.sqlite": root / "stores/market/market.sqlite",
        "runs.sqlite": root / "stores/screening/runs.sqlite",
        "macro.sqlite": root / "stores/macro/macro.sqlite",
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
        [root / "batch/scripts/r2_transfer.sh", "pull-machine"],
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
    generation_dir = Path(environment["R2_GENERATION_DIR"])
    assert not any(generation_dir.glob("*.etag"))
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

    environment = _environment(bin_dir, log)
    completed = subprocess.run(
        [root / "batch/scripts/r2_transfer.sh", "pull-machine"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert all(path.read_bytes() == b"x" for path in _machine_stores(root).values())
    generation_dir = Path(environment["R2_GENERATION_DIR"])
    assert {
        path.name: path.read_text(encoding="utf-8") for path in generation_dir.glob("*.etag")
    } == {
        "market.sqlite.etag": '"etag-stable"\n',
        "runs.sqlite.etag": '"etag-stable"\n',
        "macro.sqlite.etag": '"etag-stable"\n',
    }
    assert not list(root.glob(".r2-transfer.*"))


def test_the_transfer_script_names_the_same_pointer_key_the_engine_publishes() -> None:
    """The emptying gate reads this key; a rename would turn it off in silence.

    `_push_keys` empties the market copy only when the lake serves a release, and it
    decides that by looking for the pointer. A key that stopped matching would not
    error — it would simply never find a pointer, and every push would go back to
    carrying the full store while still reporting success.
    """

    from baibai_engine.batch_api import lake_current_l1_pointer_key

    assert lake_current_l1_pointer_key() in TRANSFER_SCRIPT.read_text(encoding="utf-8")


def test_first_publication_needs_no_sidecar_and_prints_its_result(
    tmp_path: Path,
) -> None:
    """The canonical shell path supports first publication without duplicate state."""

    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    environment = _environment(bin_dir, log)
    record = Path(environment["R2_GENERATION_DIR"]) / "lake-release.json"

    completed = subprocess.run(
        [root / "batch/scripts/r2_transfer.sh", "publish-lake"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    published = [line for line in log.read_text(encoding="utf-8").splitlines() if "publish" in line]
    assert len(published) == 1
    assert "--full-rebuild" not in published[0]
    assert "--base-release" not in published[0]
    assert "--origin-release" not in published[0]
    assert "--origin-manifest-sha256" not in published[0]
    assert "release-after-rebuild" in completed.stdout
    assert not record.exists()


def test_a_failed_publish_does_not_touch_a_legacy_sidecar(
    tmp_path: Path,
) -> None:
    """A leftover pre-marker cache is ignored rather than kept as hidden state."""

    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    environment = _environment(bin_dir, log)
    environment["PUBLISH_FAKE_EXIT"] = "1"
    record = Path(environment["R2_GENERATION_DIR"]) / "lake-release.json"
    record.write_text(
        '{"release_id": "release-before", "release_manifest_sha256": "old"}\n', encoding="utf-8"
    )

    completed = subprocess.run(
        [root / "batch/scripts/r2_transfer.sh", "publish-lake"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "release-before" in record.read_text(encoding="utf-8")


def test_partial_stdout_from_a_failed_publisher_is_never_persisted(
    tmp_path: Path,
) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    environment = _environment(bin_dir, log)
    environment["PUBLISH_FAKE_EXIT"] = "1"
    environment["PUBLISH_FAKE_PARTIAL"] = '{"release_id":"partial"'
    record = Path(environment["R2_GENERATION_DIR"]) / "lake-release.json"
    before = b'{"release_id": "release-before", "release_manifest_sha256": "old"}\n'
    record.write_bytes(before)

    completed = subprocess.run(
        [root / "batch/scripts/r2_transfer.sh", "publish-lake"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert record.read_bytes() == before


def test_an_unknown_publish_lake_argument_is_refused(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)

    completed = subprocess.run(
        [root / "batch/scripts/r2_transfer.sh", "publish-lake", "incremental-please"],
        cwd=tmp_path,
        env=_environment(bin_dir, log),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "usage:" in completed.stderr
