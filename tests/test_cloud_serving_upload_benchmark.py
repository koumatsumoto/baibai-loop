from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import tools.cloud.benchmark_serving_upload as benchmark_module
from tools.cloud.benchmark_serving_upload import (
    ARMS,
    AwsRunner,
    BenchmarkError,
    _upload_serving,
    benchmark_prefix,
    build_plan,
    cleanup_prefix,
    create_plan,
    evaluate_durations,
    run_benchmark,
    scan_export,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools/cloud/benchmark_serving_upload.py"
TRANSFER_SCRIPT = REPO_ROOT / "tools/cloud/r2_transfer.sh"
REVIEWED_SCHEDULE = (
    10,
    20,
    40,
    40,
    20,
    10,
    20,
    10,
    40,
    40,
    10,
    20,
    10,
    40,
    20,
)


def _export(root: Path, files: dict[str, bytes]) -> Path:
    for key, content in files.items():
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def _pair(tmp_path: Path) -> tuple[Path, Path]:
    before = _export(
        tmp_path / "before",
        {
            "views/meta.json": b'{"generation":"before"}',
            "views/dashboard.json": b"same",
            "views/changed.json": b"old",
            "views/removed.json": b"remove-me",
            "history/candidate-views/2026-07-30.json": b"retained-history",
        },
    )
    after = _export(
        tmp_path / "after",
        {
            "views/meta.json": b'{"generation":"after"}',
            "views/dashboard.json": b"same",
            "views/changed.json": b"new",
            "views/added.json": b"new-view",
            "history/candidate-views/2026-07-31.json": b"new-history",
        },
    )
    future = time.time() + 10_000
    os.utime(after / "views/dashboard.json", (future, future))
    return before, after


def _fake_command(
    remote_root: Path,
    command_log: list[tuple[int, tuple[str, ...]]],
    transfer_log: list[tuple[str, str, str, int]] | None = None,
):
    def resolve(value: str) -> tuple[Path, bool]:
        prefix = "s3://baibai-serving/"
        if value.startswith(prefix):
            return remote_root / value.removeprefix(prefix), True
        return Path(value), False

    def files(root: Path) -> dict[str, Path]:
        if not root.exists():
            return {}
        return {
            path.relative_to(root).as_posix(): path for path in root.rglob("*") if path.is_file()
        }

    def trial_name(path: Path) -> str | None:
        return next((part for part in path.parts if part.startswith("trial-")), None)

    def remote_key(path: Path) -> str:
        parts = path.relative_to(remote_root).parts
        assert parts[:2] == ("benchmarks", "serving-upload")
        return Path(*parts[3:]).as_posix()

    def command(self: AwsRunner, config: Path, *arguments: str) -> None:
        config_text = config.read_text(encoding="utf-8")
        concurrency = int(config_text.rsplit("=", maxsplit=1)[1].strip())
        command_log.append((concurrency, arguments))
        operation = arguments[0]
        if operation == "rm":
            target, remote = resolve(arguments[1])
            assert remote
            if target.exists():
                shutil.rmtree(target)
            return
        if operation == "cp":
            source, source_remote = resolve(arguments[1])
            destination, destination_remote = resolve(arguments[2])
            assert not source_remote
            assert destination_remote
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            os.utime(destination, (time.time(), time.time()))
            trial = trial_name(source)
            if trial is not None and transfer_log is not None:
                transfer_log.append(
                    (trial, "upload", remote_key(destination), source.stat().st_size)
                )
            if trial is not None:
                time.sleep(0.03 if concurrency == 10 else 0.001)
            return
        if operation != "sync":
            raise AssertionError(arguments)
        source, source_remote = resolve(arguments[1])
        destination, destination_remote = resolve(arguments[2])
        assert source_remote != destination_remote
        destination.mkdir(parents=True, exist_ok=True)
        excluded = None
        if "--exclude" in arguments:
            excluded = arguments[arguments.index("--exclude") + 1]
        source_files = files(source)
        destination_files = files(destination)
        trial = trial_name(source)
        if "--delete" in arguments:
            for key, path in destination_files.items():
                if excluded is not None and Path(key).name == excluded:
                    continue
                if key not in source_files:
                    if trial is not None and transfer_log is not None:
                        transfer_log.append((trial, "delete", remote_key(path), 0))
                    path.unlink()
        for key, source_path in source_files.items():
            if excluded is not None and Path(key).name == excluded:
                continue
            destination_path = destination / key
            should_copy = (
                not destination_path.exists()
                or destination_path.stat().st_size != source_path.stat().st_size
                or source_path.stat().st_mtime > destination_path.stat().st_mtime
            )
            if not should_copy:
                continue
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, destination_path)
            timestamp = source_path.stat().st_mtime if destination_remote is False else time.time()
            os.utime(destination_path, (timestamp, timestamp))
            if trial is not None and transfer_log is not None:
                transfer_log.append(
                    (trial, "upload", remote_key(destination_path), source_path.stat().st_size)
                )
        if not source_remote and trial is not None:
            time.sleep(0.03 if concurrency == 10 else 0.001)

    return command


def _fake_api(remote_root: Path, api_log: list[tuple[str, ...]]):
    def api(self: AwsRunner, *arguments: str) -> None:
        api_log.append(arguments)
        operation = arguments[0]
        key = arguments[arguments.index("--key") + 1]
        remote = remote_root / key
        if operation == "put-object":
            source = Path(arguments[arguments.index("--body") + 1])
            remote.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(
                    remote,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError as exc:
                raise BenchmarkError("conditional claim create failed") from exc
            with os.fdopen(descriptor, "wb") as destination, source.open("rb") as source_stream:
                shutil.copyfileobj(source_stream, destination)
            return
        if operation == "get-object":
            if not remote.is_file():
                raise BenchmarkError("remote claim is missing")
            destination = Path(arguments[-1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(remote, destination)
            return
        raise AssertionError(arguments)

    return api


def _credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R2_ACCOUNT_ID", "account-for-test")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "access-for-test")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret-for-test")


def test_plan_fixes_the_production_workload_and_expected_final_manifest(
    tmp_path: Path,
) -> None:
    before, after = _pair(tmp_path)

    plan, _before, _after, expected = build_plan(before, after, "20260731-local-a1")

    assert plan.arms == ARMS
    assert plan.repetitions == 5
    assert plan.schedule_id == "counterbalanced-v1"
    assert plan.schedule == REVIEWED_SCHEDULE
    assert plan.prefix == "benchmarks/serving-upload/20260731-local-a1/"
    assert plan.workload.upload_keys == (
        "history/candidate-views/2026-07-31.json",
        "views/added.json",
        "views/changed.json",
        "views/meta.json",
    )
    assert plan.workload.delete_keys == ("views/removed.json",)
    assert set(expected) == {
        "history/candidate-views/2026-07-30.json",
        "history/candidate-views/2026-07-31.json",
        "views/added.json",
        "views/changed.json",
        "views/dashboard.json",
        "views/meta.json",
    }


@pytest.mark.parametrize(
    "run_id",
    ["../views", "short", "UPPERCASE-RUN", "valid-run/child", "a" * 65],
)
def test_prefix_rejects_any_run_id_that_could_escape_isolation(run_id: str) -> None:
    with pytest.raises(BenchmarkError):
        benchmark_prefix(run_id)


def test_export_rejects_symlinks_and_unexpected_key_families(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    symlink_export = _export(tmp_path / "symlink-export", {"views/meta.json": b"{}"})
    (symlink_export / "views/link.json").symlink_to(target)
    unexpected_export = _export(
        tmp_path / "unexpected-export",
        {"views/meta.json": b"{}", "system/latest-run.json": b"{}"},
    )

    with pytest.raises(BenchmarkError, match="symlink"):
        scan_export(symlink_export)
    with pytest.raises(BenchmarkError, match="unexpected key"):
        scan_export(unexpected_export)

    root_symlink = tmp_path / "root-symlink"
    root_symlink.symlink_to(unexpected_export, target_is_directory=True)
    with pytest.raises(BenchmarkError, match="root is a symlink"):
        scan_export(root_symlink)
    with pytest.raises(BenchmarkError, match="opened safely"):
        build_plan(root_symlink, unexpected_export, "20260731-local-a1")

    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(BenchmarkError, match="opened safely"):
        build_plan(
            linked_parent / "unexpected-export",
            unexpected_export,
            "20260731-local-a1",
        )


def test_plan_command_needs_no_credentials_or_aws_access(tmp_path: Path) -> None:
    before, after = _pair(tmp_path)
    environment = dict(os.environ)
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        environment.pop(name, None)

    completed = subprocess.run(
        [
            sys.executable,
            SCRIPT,
            "plan",
            "--before-dir",
            before,
            "--after-dir",
            after,
            "--run-id",
            "20260731-local-a1",
        ],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert '"plan_digest":' in completed.stdout
    assert '"prefix": "benchmarks/serving-upload/20260731-local-a1/"' in completed.stdout


def test_run_refuses_exports_that_no_longer_match_the_reviewed_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = _pair(tmp_path)
    plan_file = tmp_path / "plan.json"
    create_plan(
        before_dir=before,
        after_dir=after,
        run_id="20260731-local-a1",
        output=plan_file,
    )
    (after / "views/added.json").write_bytes(b"changed-after-review")
    commands: list[tuple[int, tuple[str, ...]]] = []
    monkeypatch.setattr(AwsRunner, "command", _fake_command(tmp_path / "remote", commands))

    with pytest.raises(BenchmarkError, match="reviewed plan does not match"):
        run_benchmark(
            before_dir=before,
            after_dir=after,
            run_id="20260731-local-a1",
            plan_file=plan_file,
            claim_file=tmp_path / "claim.json",
            output=tmp_path / "report.json",
            bucket="baibai-serving",
        )

    assert commands == []


def test_secure_freeze_rejects_a_file_swapped_to_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = _pair(tmp_path)
    secret = tmp_path / "secret"
    secret.write_text("must-not-be-read", encoding="utf-8")
    meta = before / "views/meta.json"
    original_open_child = benchmark_module._open_child
    swapped = False

    def swap_then_open(parent_fd: int, name: str, flags: int) -> int:
        nonlocal swapped
        if name == "meta.json" and not swapped:
            swapped = True
            meta.unlink()
            meta.symlink_to(secret)
        return original_open_child(parent_fd, name, flags)

    monkeypatch.setattr(benchmark_module, "_open_child", swap_then_open)

    with pytest.raises(BenchmarkError, match="changed while freezing"):
        build_plan(before, after, "20260731-local-a1")


def test_atomic_plan_write_does_not_follow_a_predictable_temp_symlink(
    tmp_path: Path,
) -> None:
    before, after = _pair(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("unchanged", encoding="utf-8")
    output = tmp_path / "plan.json"
    predictable = tmp_path / ".plan.json.tmp"
    predictable.symlink_to(victim)

    create_plan(
        before_dir=before,
        after_dir=after,
        run_id="20260731-local-a1",
        output=output,
    )

    assert victim.read_text(encoding="utf-8") == "unchanged"
    assert output.is_file()
    assert not output.is_symlink()


def test_aws_failure_redacts_endpoint_and_credentials(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "aws"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s %s %s %s\\n\' "$*" "$R2_ACCESS_KEY_ID" '
        '"$R2_SECRET_ACCESS_KEY" "$R2_ACCOUNT_ID" >&2\n'
        "exit 9\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "R2_ACCESS_KEY_ID": "access-for-test",
        "R2_SECRET_ACCESS_KEY": "secret-for-test",
        "R2_ACCOUNT_ID": "account-for-test",
    }
    runner = AwsRunner(
        bucket="baibai-serving",
        account_id="account-for-test",
        base_env=environment,
    )

    with pytest.raises(BenchmarkError) as error:
        runner.command(tmp_path / "config", "sync", "/source/", runner.uri("safe/"))

    rendered = str(error.value)
    assert "access-for-test" not in rendered
    assert "secret-for-test" not in rendered
    assert "account-for-test" not in rendered


def test_duration_evaluation_uses_p50_and_exact_thirty_percent_boundary() -> None:
    p50, threshold, recommended = evaluate_durations(
        {
            10: [10.0, 10.0, 10.0, 10.0, 100.0],
            20: [7.0001] * 5,
            40: [7.0] * 5,
        }
    )

    assert p50 == {"10": 10.0, "20": 7.0001, "40": 7.0}
    assert threshold == 7.0
    assert recommended == 40

    _p50, _threshold, smallest = evaluate_durations({10: [10.0] * 5, 20: [7.0] * 5, 40: [6.0] * 5})
    assert smallest == 20


def test_full_fifteen_trial_run_is_isolated_verified_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = _pair(tmp_path)
    original = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for root in (before, after)
        for path in root.rglob("*")
        if path.is_file()
    }
    remote_root = tmp_path / "remote"
    commands: list[tuple[int, tuple[str, ...]]] = []
    api_commands: list[tuple[str, ...]] = []
    transfers: list[tuple[str, str, str, int]] = []
    monkeypatch.setattr(AwsRunner, "command", _fake_command(remote_root, commands, transfers))
    monkeypatch.setattr(AwsRunner, "version", lambda self: "aws-cli/test")
    _credentials(monkeypatch)
    plan_file = tmp_path / "plan.json"
    plan_document = create_plan(
        before_dir=before,
        after_dir=after,
        run_id="20260731-local-a1",
        output=plan_file,
    )
    monkeypatch.setattr(AwsRunner, "api", _fake_api(remote_root, api_commands))
    output = tmp_path / "report.json"
    claim_file = tmp_path / "claim.json"

    report = run_benchmark(
        before_dir=before,
        after_dir=after,
        run_id="20260731-local-a1",
        plan_file=plan_file,
        claim_file=claim_file,
        output=output,
        bucket="baibai-serving",
    )

    assert output.is_file()
    assert len(report["trials"]) == 15
    assert {arm: sum(t["concurrency"] == arm for t in report["trials"]) for arm in ARMS} == {
        10: 5,
        20: 5,
        40: 5,
    }
    assert [trial["concurrency"] for trial in report["trials"]] == list(REVIEWED_SCHEDULE)
    assert len({trial["planned_workload_digest"] for trial in report["trials"]}) == 1
    assert {trial["reset_manifest_digest"] for trial in report["trials"]} == {
        report["plan"]["before_manifest_digest"]
    }
    assert all(
        trial["verified_manifest_digest"] == report["plan"]["expected_manifest_digest"]
        for trial in report["trials"]
    )
    assert report["plan_digest"] == plan_document["plan_digest"]
    assert report["aws_cli_version"] == "aws-cli/test"
    assert claim_file.is_file()
    assert report["recommended_concurrency"] == 20
    assert report["production_config_changed"] is False
    assert report["prefix_cleaned"] is True
    assert not (remote_root / "benchmarks/serving-upload/20260731-local-a1").exists()
    assert commands[-2][1] == (
        "rm",
        "s3://baibai-serving/benchmarks/serving-upload/20260731-local-a1/",
        "--recursive",
    )
    assert commands[-1][1][:2] == (
        "sync",
        "s3://baibai-serving/benchmarks/serving-upload/20260731-local-a1/",
    )
    assert api_commands[0][0] == "put-object"
    assert api_commands[0][-2:] == ("--if-none-match", "*")
    assert api_commands[-1][0] == "get-object"
    timed_commands = [
        (concurrency, arguments)
        for concurrency, arguments in commands
        if any("trial-" in argument and "/after/" in argument for argument in arguments)
    ]
    assert [concurrency for concurrency, _arguments in timed_commands] == [
        arm for arm in REVIEWED_SCHEDULE for _operation in range(3)
    ]
    for offset in range(0, len(timed_commands), 3):
        trial_commands = [
            arguments for _concurrency, arguments in timed_commands[offset : offset + 3]
        ]
        assert [arguments[0] for arguments in trial_commands] == ["sync", "sync", "cp"]
        assert trial_commands[0][-3:] == ("--delete", "--exclude", "meta.json")
        assert "--delete" not in trial_commands[1]
        assert trial_commands[2][2].endswith("/views/meta.json")
    assert all(
        concurrency == 10
        for concurrency, arguments in commands
        if not any("trial-" in argument and "/after/" in argument for argument in arguments)
    )
    transfers_by_trial: dict[str, set[tuple[str, str, int]]] = defaultdict(set)
    for trial, operation, key, size in transfers:
        transfers_by_trial[trial].add((operation, key, size))
    expected_transfers = {
        ("upload", key, (after / key).stat().st_size)
        for key in report["plan"]["workload"]["upload_keys"]
    } | {("delete", key, 0) for key in report["plan"]["workload"]["delete_keys"]}
    assert set(transfers_by_trial) == {f"trial-{sequence:02d}" for sequence in range(1, 16)}
    assert all(actual == expected_transfers for actual in transfers_by_trial.values())
    assert len(transfers) == len(expected_transfers) * 15
    for path, identity in original.items():
        assert (path.read_bytes(), path.stat().st_mtime_ns) == identity


def test_run_refuses_a_preexisting_run_prefix_without_deleting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = _pair(tmp_path)
    plan_file = tmp_path / "plan.json"
    create_plan(
        before_dir=before,
        after_dir=after,
        run_id="20260731-local-a1",
        output=plan_file,
    )
    remote_root = tmp_path / "remote"
    existing = remote_root / "benchmarks/serving-upload/20260731-local-a1/views/meta.json"
    existing.parent.mkdir(parents=True)
    existing.write_text('{"owner":"another-run"}', encoding="utf-8")
    commands: list[tuple[int, tuple[str, ...]]] = []
    monkeypatch.setattr(AwsRunner, "command", _fake_command(remote_root, commands))
    monkeypatch.setattr(AwsRunner, "version", lambda self: "aws-cli/test")
    _credentials(monkeypatch)

    with pytest.raises(BenchmarkError, match="already contains objects"):
        run_benchmark(
            before_dir=before,
            after_dir=after,
            run_id="20260731-local-a1",
            plan_file=plan_file,
            claim_file=tmp_path / "claim.json",
            output=tmp_path / "report.json",
            bucket="baibai-serving",
        )

    assert existing.read_text(encoding="utf-8") == '{"owner":"another-run"}'
    assert all(arguments[0] != "rm" for _concurrency, arguments in commands)


def test_meta_is_not_published_when_history_upload_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _before, after = _pair(tmp_path)
    runner = AwsRunner(
        bucket="baibai-serving",
        account_id="account-for-test",
        base_env=dict(os.environ),
    )
    calls: list[tuple[str, ...]] = []

    def fail_history(_config: Path, *arguments: str) -> None:
        calls.append(arguments)
        if "history/candidate-views/" in arguments[2]:
            raise BenchmarkError("history failed")

    monkeypatch.setattr(runner, "command", fail_history)

    with pytest.raises(BenchmarkError, match="history failed"):
        _upload_serving(runner, tmp_path / "config", after, runner.uri("isolated/"))

    assert [arguments[0] for arguments in calls] == ["sync", "sync"]


def test_benchmark_upload_commands_match_the_production_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _before, after = _pair(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "aws.log"
    executable = bin_dir / "aws"
    executable.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$AWS_LOG"\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AWS_LOG": str(log),
        "R2_ACCOUNT_ID": "account-for-test",
        "R2_ACCESS_KEY_ID": "access-for-test",
        "R2_SECRET_ACCESS_KEY": "secret-for-test",
    }
    subprocess.run(
        [TRANSFER_SCRIPT, "upload-serving", after],
        cwd=REPO_ROOT,
        env=environment,
        check=True,
    )
    production = [tuple(shlex.split(line)) for line in log.read_text(encoding="utf-8").splitlines()]

    benchmark: list[tuple[str, ...]] = []
    runner = AwsRunner(
        bucket="baibai-serving",
        account_id="account-for-test",
        base_env=environment,
    )
    monkeypatch.setattr(
        runner,
        "command",
        lambda _config, *arguments: benchmark.append(arguments),
    )
    _upload_serving(runner, tmp_path / "config", after, runner.uri("isolated/"))

    def normalize(arguments: tuple[str, ...]) -> tuple[str, str, str, tuple[str, ...]]:
        values = list(arguments)
        if values[0] == "s3":
            values.pop(0)
        if "--endpoint-url" in values:
            values = values[: values.index("--endpoint-url")]
        operation, source, destination, *options = values

        def family(value: str) -> str:
            if value.endswith("/views/meta.json"):
                return "meta"
            if "/history/candidate-views/" in value:
                return "history"
            if value.endswith("/views/"):
                return "views"
            raise AssertionError(value)

        return operation, family(source), family(destination), tuple(options)

    assert [normalize(command) for command in benchmark] == [
        normalize(command) for command in production
    ]


def test_cleanup_removes_only_the_validated_run_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote_root = tmp_path / "remote"
    target = remote_root / "benchmarks/serving-upload/20260731-local-a1/views/meta.json"
    sibling = remote_root / "benchmarks/serving-upload/20260731-local-b2/views/meta.json"
    target.parent.mkdir(parents=True)
    sibling.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    sibling.write_text("{}", encoding="utf-8")
    claim_payload = {
        "schema_version": 1,
        "run_id": "20260731-local-a1",
        "plan_digest": "a" * 64,
        "owner_token": "b" * 64,
    }
    claim_file = tmp_path / "claim.json"
    claim_file.write_text(json.dumps(claim_payload), encoding="utf-8")
    remote_claim = target.parents[1] / "_benchmark-claim.json"
    remote_claim.write_text(json.dumps(claim_payload), encoding="utf-8")
    commands: list[tuple[int, tuple[str, ...]]] = []
    api_commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(AwsRunner, "command", _fake_command(remote_root, commands))
    monkeypatch.setattr(AwsRunner, "api", _fake_api(remote_root, api_commands))
    _credentials(monkeypatch)

    cleanup_prefix(
        run_id="20260731-local-a1",
        claim_file=claim_file,
        bucket="baibai-serving",
    )

    assert not target.exists()
    assert sibling.exists()


def test_conditional_claim_allows_only_one_owner_for_a_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote_root = tmp_path / "remote"
    api_commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(AwsRunner, "api", _fake_api(remote_root, api_commands))
    runner = AwsRunner(
        bucket="baibai-serving",
        account_id="account-for-test",
        base_env=dict(os.environ),
    )
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text('{"owner":"first"}', encoding="utf-8")
    second.write_text('{"owner":"second"}', encoding="utf-8")
    prefix = benchmark_prefix("20260731-local-a1")
    barrier = Barrier(2)

    def create(source: Path) -> str:
        barrier.wait()
        try:
            benchmark_module._create_remote_claim(runner, prefix, source)
        except BenchmarkError:
            return "rejected"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(create, (first, second)))

    remote_claim = remote_root / prefix / "_benchmark-claim.json"
    assert sorted(outcomes) == ["created", "rejected"]
    assert remote_claim.read_text(encoding="utf-8") in {
        '{"owner":"first"}',
        '{"owner":"second"}',
    }
    assert all(command[-2:] == ("--if-none-match", "*") for command in api_commands)


def test_local_claim_file_can_have_only_one_owner_under_concurrency(tmp_path: Path) -> None:
    path = tmp_path / "claim.json"
    claims = (
        benchmark_module.Claim(1, "20260731-local-a1", "a" * 64, "b" * 64),
        benchmark_module.Claim(1, "20260731-local-a1", "a" * 64, "c" * 64),
    )
    barrier = Barrier(2)

    def write(claim: benchmark_module.Claim) -> str:
        barrier.wait()
        try:
            benchmark_module._write_claim_exclusive(path, claim)
        except BenchmarkError:
            return "rejected"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write, claims))

    assert sorted(outcomes) == ["created", "rejected"]
    stored = benchmark_module._read_claim(path)
    assert stored in claims


def test_concurrent_runs_allow_only_the_local_claim_owner_to_mutate_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = _pair(tmp_path)
    plan_file = tmp_path / "plan.json"
    create_plan(
        before_dir=before,
        after_dir=after,
        run_id="20260731-local-a1",
        output=plan_file,
    )
    remote_root = tmp_path / "remote"
    commands: list[tuple[int, tuple[str, ...]]] = []
    api_commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(AwsRunner, "command", _fake_command(remote_root, commands))
    monkeypatch.setattr(AwsRunner, "api", _fake_api(remote_root, api_commands))
    monkeypatch.setattr(AwsRunner, "version", lambda self: "aws-cli/test")
    _credentials(monkeypatch)
    claim_file = tmp_path / "shared-claim.json"
    barrier = Barrier(2)
    original_write = benchmark_module._write_claim_exclusive

    def synchronized_write(path: Path, claim: benchmark_module.Claim) -> None:
        barrier.wait()
        original_write(path, claim)

    monkeypatch.setattr(benchmark_module, "_write_claim_exclusive", synchronized_write)

    def run(index: int) -> tuple[str, dict[str, object] | None]:
        try:
            report = run_benchmark(
                before_dir=before,
                after_dir=after,
                run_id="20260731-local-a1",
                plan_file=plan_file,
                claim_file=claim_file,
                output=tmp_path / f"report-{index}.json",
                bucket="baibai-serving",
            )
        except BenchmarkError:
            return "rejected", None
        return "completed", report

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, (1, 2)))

    assert sorted(outcome for outcome, _report in results) == ["completed", "rejected"]
    completed = next(report for outcome, report in results if outcome == "completed")
    assert completed is not None
    assert completed["prefix_cleaned"] is True
    assert sum(arguments[0] == "rm" for _concurrency, arguments in commands) == 1
    assert (
        sum(
            any("trial-" in argument and "/after/" in argument for argument in arguments)
            for _concurrency, arguments in commands
        )
        == 45
    )
    assert sum(arguments[0] == "put-object" for arguments in api_commands) == 1
    assert not (remote_root / benchmark_prefix("20260731-local-a1")).exists()


def test_cleanup_refuses_a_claim_owned_by_another_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote_root = tmp_path / "remote"
    prefix = benchmark_prefix("20260731-local-a1")
    remote_claim = remote_root / prefix / "_benchmark-claim.json"
    remote_claim.parent.mkdir(parents=True)
    local_payload = {
        "schema_version": 1,
        "run_id": "20260731-local-a1",
        "plan_digest": "a" * 64,
        "owner_token": "b" * 64,
    }
    remote_payload = {**local_payload, "owner_token": "c" * 64}
    claim_file = tmp_path / "claim.json"
    claim_file.write_text(json.dumps(local_payload), encoding="utf-8")
    remote_claim.write_text(json.dumps(remote_payload), encoding="utf-8")
    commands: list[tuple[int, tuple[str, ...]]] = []
    monkeypatch.setattr(AwsRunner, "command", _fake_command(remote_root, commands))
    monkeypatch.setattr(AwsRunner, "api", _fake_api(remote_root, []))
    _credentials(monkeypatch)

    with pytest.raises(BenchmarkError, match="different claim"):
        cleanup_prefix(
            run_id="20260731-local-a1",
            claim_file=claim_file,
            bucket="baibai-serving",
        )

    assert remote_claim.is_file()
    assert commands == []
