#!/usr/bin/env python3
"""Benchmark isolated serving uploads without touching Worker-visible R2 keys."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ARMS = (10, 20, 40)
SCHEDULE = (
    (10, 20, 40),
    (40, 20, 10),
    (20, 10, 40),
    (40, 10, 20),
    (10, 40, 20),
)
PREFIX_ROOT = "benchmarks/serving-upload"
PROFILE = "baibai-serving-upload-benchmark"
UPLOAD_CONTRACT_VERSION = "serving-upload-v1"
SCHEDULE_ID = "counterbalanced-v1"
CLAIM_KEY = "_benchmark-claim.json"
RUN_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{7,63}")
BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9_-]{3,64}")


class BenchmarkError(ValueError):
    """The benchmark input or isolated remote state violates the safety contract."""


@dataclass(frozen=True)
class FileIdentity:
    key: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Workload:
    upload_keys: tuple[str, ...]
    delete_keys: tuple[str, ...]
    upload_bytes: int
    digest: str


@dataclass(frozen=True)
class BenchmarkPlan:
    run_id: str
    prefix: str
    before_manifest_digest: str
    after_manifest_digest: str
    expected_manifest_digest: str
    expected_key_count: int
    workload: Workload
    upload_contract_version: str = UPLOAD_CONTRACT_VERSION
    schedule_id: str = SCHEDULE_ID
    schedule: tuple[int, ...] = tuple(arm for row in SCHEDULE for arm in row)
    arms: tuple[int, ...] = ARMS
    repetitions: int = len(SCHEDULE)


@dataclass(frozen=True)
class TrialResult:
    sequence: int
    repetition: int
    concurrency: int
    elapsed_seconds: float
    verified_key_count: int
    verified_manifest_digest: str
    reset_manifest_digest: str
    planned_workload_digest: str


@dataclass(frozen=True)
class Claim:
    schema_version: int
    run_id: str
    plan_digest: str
    owner_token: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _allowed_key(key: str) -> bool:
    return key.startswith(("views/", "history/candidate-views/"))


def scan_export(root: Path) -> dict[str, FileIdentity]:
    if root.is_symlink():
        raise BenchmarkError(f"serving export root is a symlink: {root}")
    if not root.is_dir():
        raise BenchmarkError(f"serving export is not a directory: {root}")
    manifest: dict[str, FileIdentity] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BenchmarkError(f"serving export contains a symlink: {path}")
        if not path.is_file():
            continue
        key = path.relative_to(root).as_posix()
        if not _allowed_key(key):
            raise BenchmarkError(f"serving export contains an unexpected key: {key}")
        manifest[key] = FileIdentity(key=key, sha256=_sha256(path), size=path.stat().st_size)
    if "views/meta.json" not in manifest:
        raise BenchmarkError(f"serving export is missing views/meta.json: {root}")
    return manifest


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _validate_source_permissions(value: os.stat_result, label: str) -> None:
    if value.st_uid != os.geteuid():
        raise BenchmarkError(f"serving export entry is not owned by the current user: {label}")
    if value.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise BenchmarkError(f"serving export entry is writable by another user: {label}")


def _open_child(parent_fd: int, name: str, flags: int) -> int:
    return os.open(name, flags, dir_fd=parent_fd)


def _open_directory_path(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd = os.open("/" if path.is_absolute() else ".", flags)
    try:
        for component in path.parts:
            if component in ("", ".", "/"):
                continue
            if component == "..":
                raise BenchmarkError(f"serving export path cannot contain '..': {path}")
            next_fd = _open_child(current_fd, component, flags)
            os.close(current_fd)
            current_fd = next_fd
    except BaseException:
        os.close(current_fd)
        raise
    return current_fd


def _copy_directory_fd(source_fd: int, destination: Path, relative: Path) -> None:
    directory_before = os.fstat(source_fd)
    _validate_source_permissions(directory_before, relative.as_posix() or ".")
    names = sorted(os.listdir(source_fd))
    for name in names:
        child_relative = relative / name
        label = child_relative.as_posix()
        try:
            entry_before = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        except OSError as exc:
            raise BenchmarkError(f"serving export changed while freezing: {label}") from exc
        _validate_source_permissions(entry_before, label)
        destination_path = destination / name
        if stat.S_ISLNK(entry_before.st_mode):
            raise BenchmarkError(f"serving export contains a symlink: {label}")
        if stat.S_ISDIR(entry_before.st_mode):
            try:
                child_fd = _open_child(
                    source_fd,
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
            except OSError as exc:
                raise BenchmarkError(f"serving export changed while freezing: {label}") from exc
            try:
                if _stat_identity(os.fstat(child_fd)) != _stat_identity(entry_before):
                    raise BenchmarkError(f"serving export changed while freezing: {label}")
                destination_path.mkdir(mode=0o700)
                _copy_directory_fd(child_fd, destination_path, child_relative)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(entry_before.st_mode):
            try:
                child_fd = _open_child(source_fd, name, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as exc:
                raise BenchmarkError(f"serving export changed while freezing: {label}") from exc
            try:
                if _stat_identity(os.fstat(child_fd)) != _stat_identity(entry_before):
                    raise BenchmarkError(f"serving export changed while freezing: {label}")
                with (
                    os.fdopen(child_fd, "rb", closefd=False) as source_stream,
                    destination_path.open("xb") as destination_stream,
                ):
                    shutil.copyfileobj(source_stream, destination_stream)
                entry_after_read = os.fstat(child_fd)
            finally:
                os.close(child_fd)
            if _stat_identity(entry_after_read) != _stat_identity(entry_before):
                raise BenchmarkError(f"serving export changed while freezing: {label}")
        else:
            raise BenchmarkError(f"serving export contains a non-regular entry: {label}")
        try:
            entry_after = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        except OSError as exc:
            raise BenchmarkError(f"serving export changed while freezing: {label}") from exc
        if _stat_identity(entry_after) != _stat_identity(entry_before):
            raise BenchmarkError(f"serving export changed while freezing: {label}")
    if sorted(os.listdir(source_fd)) != names:
        raise BenchmarkError(f"serving export directory changed while freezing: {relative}")
    if _stat_identity(os.fstat(source_fd)) != _stat_identity(directory_before):
        raise BenchmarkError(f"serving export directory changed while freezing: {relative}")


def _freeze_export(source: Path, destination: Path) -> dict[str, FileIdentity]:
    destination.mkdir(mode=0o700)
    try:
        source_fd = _open_directory_path(source)
    except OSError as exc:
        raise BenchmarkError(f"serving export cannot be opened safely: {source}") from exc
    try:
        _copy_directory_fd(source_fd, destination, Path())
    finally:
        os.close(source_fd)
    return scan_export(destination)


def _manifest_digest(manifest: dict[str, FileIdentity]) -> str:
    digest = hashlib.sha256()
    for key in sorted(manifest):
        identity = manifest[key]
        digest.update(f"{identity.key}\0{identity.sha256}\0{identity.size}\n".encode())
    return digest.hexdigest()


def _expected_manifest(
    before: dict[str, FileIdentity], after: dict[str, FileIdentity]
) -> dict[str, FileIdentity]:
    expected = {key: identity for key, identity in before.items() if not key.startswith("views/")}
    expected.update(after)
    return expected


def _workload(before: dict[str, FileIdentity], after: dict[str, FileIdentity]) -> Workload:
    uploads: set[str] = {"views/meta.json"}
    for key, identity in after.items():
        if key == "views/meta.json":
            continue
        previous = before.get(key)
        if previous is None or previous.sha256 != identity.sha256:
            uploads.add(key)
    deletes = {key for key in before if key.startswith("views/") and key not in after}
    ordered_uploads = tuple(sorted(uploads))
    ordered_deletes = tuple(sorted(deletes))
    upload_bytes = sum(after[key].size for key in ordered_uploads)
    digest = hashlib.sha256()
    for operation, keys in (("upload", ordered_uploads), ("delete", ordered_deletes)):
        for key in keys:
            candidate = after.get(key)
            payload = "" if candidate is None else f"{candidate.sha256}:{candidate.size}"
            digest.update(f"{operation}\0{key}\0{payload}\n".encode())
    return Workload(
        upload_keys=ordered_uploads,
        delete_keys=ordered_deletes,
        upload_bytes=upload_bytes,
        digest=digest.hexdigest(),
    )


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise BenchmarkError("run-id must be 8-64 lowercase alphanumeric/hyphen characters")
    return run_id


def benchmark_prefix(run_id: str) -> str:
    return f"{PREFIX_ROOT}/{validate_run_id(run_id)}/"


def _validate_schedule() -> None:
    expected_arms = set(ARMS)
    if any(len(row) != len(ARMS) or set(row) != expected_arms for row in SCHEDULE):
        raise BenchmarkError("every benchmark schedule round must be a permutation of all arms")
    for arm in ARMS:
        position_counts = [
            sum(row[position] == arm for row in SCHEDULE) for position in range(len(ARMS))
        ]
        if max(position_counts) - min(position_counts) > 1:
            raise BenchmarkError("benchmark schedule position balance differs by more than one")


def _build_plan_from_frozen(
    before_dir: Path, after_dir: Path, run_id: str
) -> tuple[
    BenchmarkPlan,
    dict[str, FileIdentity],
    dict[str, FileIdentity],
    dict[str, FileIdentity],
]:
    _validate_schedule()
    before = scan_export(before_dir)
    after = scan_export(after_dir)
    expected = _expected_manifest(before, after)
    plan = BenchmarkPlan(
        run_id=validate_run_id(run_id),
        prefix=benchmark_prefix(run_id),
        before_manifest_digest=_manifest_digest(before),
        after_manifest_digest=_manifest_digest(after),
        expected_manifest_digest=_manifest_digest(expected),
        expected_key_count=len(expected),
        workload=_workload(before, after),
    )
    return plan, before, after, expected


def build_plan(
    before_dir: Path, after_dir: Path, run_id: str
) -> tuple[
    BenchmarkPlan,
    dict[str, FileIdentity],
    dict[str, FileIdentity],
    dict[str, FileIdentity],
]:
    with tempfile.TemporaryDirectory(prefix="baibai-serving-upload-plan-") as temporary:
        workspace = Path(temporary)
        frozen_before = workspace / "before"
        frozen_after = workspace / "after"
        _freeze_export(before_dir, frozen_before)
        _freeze_export(after_dir, frozen_after)
        return _build_plan_from_frozen(frozen_before, frozen_after, run_id)


def _copy_with_mtimes(
    source: Path,
    destination: Path,
    *,
    future_keys: set[str],
    future_timestamp: float,
) -> None:
    shutil.copytree(source, destination)
    for path in destination.rglob("*"):
        if not path.is_file():
            continue
        key = path.relative_to(destination).as_posix()
        timestamp = future_timestamp if key in future_keys else 1.0
        os.utime(path, (timestamp, timestamp))


def _write_aws_config(path: Path, concurrency: int) -> None:
    path.write_text(
        "\n".join(
            (
                f"[profile {PROFILE}]",
                "region = auto",
                "s3 =",
                f"    max_concurrent_requests = {concurrency}",
                "",
            )
        ),
        encoding="utf-8",
    )


class AwsRunner:
    def __init__(self, *, bucket: str, account_id: str, base_env: dict[str, str]) -> None:
        if not BUCKET_PATTERN.fullmatch(bucket):
            raise BenchmarkError(f"invalid R2 serving bucket: {bucket}")
        if not ACCOUNT_PATTERN.fullmatch(account_id):
            raise BenchmarkError("invalid R2 account id")
        self.bucket = bucket
        self.endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        self.base_env = base_env

    def command(self, config: Path, *arguments: str) -> None:
        environment = {
            **self.base_env,
            "AWS_CONFIG_FILE": str(config),
            "AWS_PROFILE": PROFILE,
            "AWS_DEFAULT_REGION": "auto",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
        completed = subprocess.run(
            [
                "aws",
                "s3",
                *arguments,
                "--endpoint-url",
                self.endpoint,
                "--only-show-errors",
                "--no-progress",
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise BenchmarkError(
                f"aws s3 {arguments[0]} failed with exit status {completed.returncode}"
            )

    def api(self, *arguments: str) -> None:
        completed = subprocess.run(
            ["aws", "s3api", *arguments, "--endpoint-url", self.endpoint],
            env={
                **self.base_env,
                "AWS_DEFAULT_REGION": "auto",
                "AWS_EC2_METADATA_DISABLED": "true",
            },
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise BenchmarkError(
                f"aws s3api {arguments[0]} failed with exit status {completed.returncode}"
            )

    def version(self) -> str:
        completed = subprocess.run(
            ["aws", "--version"],
            env=self.base_env,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise BenchmarkError(f"aws --version failed with exit status {completed.returncode}")
        version = (completed.stdout or completed.stderr).strip()
        if not version:
            raise BenchmarkError("aws --version returned no version")
        return version

    def uri(self, prefix: str) -> str:
        return f"s3://{self.bucket}/{prefix}"


def _aws_environment() -> tuple[str, dict[str, str]]:
    required = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise BenchmarkError(f"missing R2 environment: {', '.join(missing)}")
    environment = {
        **os.environ,
        "AWS_ACCESS_KEY_ID": os.environ["R2_ACCESS_KEY_ID"],
        "AWS_SECRET_ACCESS_KEY": os.environ["R2_SECRET_ACCESS_KEY"],
    }
    return os.environ["R2_ACCOUNT_ID"], environment


def _verify_remote(
    runner: AwsRunner,
    config: Path,
    prefix: str,
    destination: Path,
    expected: dict[str, FileIdentity],
    expected_claim: Claim,
) -> tuple[int, str]:
    destination.mkdir(parents=True)
    runner.command(config, "sync", runner.uri(prefix), f"{destination}/", "--delete")
    claim_path = destination / CLAIM_KEY
    if _read_claim(claim_path) != expected_claim:
        raise BenchmarkError("benchmark prefix is owned by a different claim")
    claim_path.unlink()
    actual = scan_export(destination)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(key for key in set(actual) & set(expected) if actual[key] != expected[key])
        raise BenchmarkError(
            f"remote verification failed: missing={missing}, extra={extra}, changed={changed}"
        )
    return len(actual), _manifest_digest(actual)


def _verify_remote_empty(
    runner: AwsRunner,
    config: Path,
    prefix: str,
    destination: Path,
    *,
    purpose: str,
) -> None:
    destination.mkdir(parents=True)
    runner.command(config, "sync", runner.uri(prefix), f"{destination}/", "--delete")
    remaining = sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() or path.is_symlink()
    )
    if remaining:
        raise BenchmarkError(f"benchmark prefix {purpose}: {remaining}")


def _write_json(path: Path | None, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path is None:
        print(rendered, end="")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        try:
            stream = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _plan_document(plan: BenchmarkPlan) -> dict[str, Any]:
    plan_payload = json.loads(json.dumps(asdict(plan), sort_keys=True))
    canonical = json.dumps(plan_payload, separators=(",", ":"), sort_keys=True).encode()
    return {
        "schema_version": 1,
        "plan_digest": hashlib.sha256(canonical).hexdigest(),
        "plan": plan_payload,
    }


def create_plan(
    *, before_dir: Path, after_dir: Path, run_id: str, output: Path | None
) -> dict[str, Any]:
    plan, _before, _after, _expected = build_plan(before_dir, after_dir, run_id)
    document = _plan_document(plan)
    _write_json(output, document)
    return document


def _read_plan(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"benchmark plan cannot be read: {path}") from exc
    if not isinstance(payload, dict):
        raise BenchmarkError("benchmark plan must be a JSON object")
    return payload


def _claim_document(claim: Claim) -> dict[str, Any]:
    return asdict(claim)


def _new_claim(run_id: str, plan_digest: str) -> Claim:
    return Claim(
        schema_version=1,
        run_id=validate_run_id(run_id),
        plan_digest=plan_digest,
        owner_token=secrets.token_hex(32),
    )


def _write_claim_exclusive(path: Path, claim: Claim) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = (
        json.dumps(_claim_document(claim), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError as exc:
        raise BenchmarkError(f"refusing to overwrite benchmark claim file: {path}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(rendered)
        stream.flush()
        os.fsync(stream.fileno())


def _read_claim(path: Path) -> Claim:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"benchmark claim cannot be read: {path}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "run_id",
        "plan_digest",
        "owner_token",
    }:
        raise BenchmarkError("benchmark claim has an unexpected schema")
    if (
        type(payload["schema_version"]) is not int
        or not isinstance(payload["run_id"], str)
        or not isinstance(payload["plan_digest"], str)
        or not isinstance(payload["owner_token"], str)
    ):
        raise BenchmarkError("benchmark claim has invalid field types")
    try:
        claim = Claim(**payload)
    except TypeError as exc:
        raise BenchmarkError("benchmark claim has invalid field types") from exc
    if claim.schema_version != 1:
        raise BenchmarkError("benchmark claim has an unsupported schema version")
    validate_run_id(claim.run_id)
    if not re.fullmatch(r"[0-9a-f]{64}", claim.plan_digest):
        raise BenchmarkError("benchmark claim has an invalid plan digest")
    if not re.fullmatch(r"[0-9a-f]{64}", claim.owner_token):
        raise BenchmarkError("benchmark claim has an invalid owner token")
    return claim


def _create_remote_claim(runner: AwsRunner, prefix: str, claim_file: Path) -> None:
    runner.api(
        "put-object",
        "--bucket",
        runner.bucket,
        "--key",
        f"{prefix}{CLAIM_KEY}",
        "--body",
        str(claim_file),
        "--content-type",
        "application/json",
        "--if-none-match",
        "*",
    )


def _assert_remote_claim(
    runner: AwsRunner, prefix: str, expected: Claim, destination: Path
) -> None:
    runner.api(
        "get-object",
        "--bucket",
        runner.bucket,
        "--key",
        f"{prefix}{CLAIM_KEY}",
        str(destination),
    )
    if _read_claim(destination) != expected:
        raise BenchmarkError("benchmark prefix is owned by a different claim")


def _upload_serving(runner: AwsRunner, config: Path, upload_dir: Path, remote_uri: str) -> None:
    runner.command(
        config,
        "sync",
        f"{upload_dir}/views/",
        f"{remote_uri}views/",
        "--delete",
        "--exclude",
        "meta.json",
    )
    history = upload_dir / "history/candidate-views"
    if history.is_dir():
        runner.command(
            config,
            "sync",
            f"{history}/",
            f"{remote_uri}history/candidate-views/",
        )
    runner.command(
        config,
        "cp",
        str(upload_dir / "views/meta.json"),
        f"{remote_uri}views/meta.json",
    )


def evaluate_durations(
    durations: dict[int, list[float]],
) -> tuple[dict[str, float], float, int | None]:
    if set(durations) != set(ARMS) or any(len(durations[arm]) != len(SCHEDULE) for arm in ARMS):
        raise BenchmarkError("benchmark duration set does not contain five trials per arm")
    p50_seconds = {str(arm): statistics.median(durations[arm]) for arm in ARMS}
    baseline = p50_seconds[str(ARMS[0])]
    threshold = baseline * 0.70
    eligible = [arm for arm in ARMS[1:] if p50_seconds[str(arm)] <= threshold]
    return p50_seconds, threshold, min(eligible) if eligible else None


def run_benchmark(
    *,
    before_dir: Path,
    after_dir: Path,
    run_id: str,
    plan_file: Path,
    claim_file: Path,
    output: Path,
    bucket: str,
) -> dict[str, Any]:
    trials: list[TrialResult] = []
    repetitions = dict.fromkeys(ARMS, 0)
    cleaned = False
    with tempfile.TemporaryDirectory(prefix="baibai-serving-upload-benchmark-") as temporary:
        workspace = Path(temporary)
        frozen_before = workspace / "frozen-before"
        frozen_after = workspace / "frozen-after"
        _freeze_export(before_dir, frozen_before)
        _freeze_export(after_dir, frozen_after)
        plan, before, _after, expected = _build_plan_from_frozen(
            frozen_before, frozen_after, run_id
        )
        plan_document = _plan_document(plan)
        if _read_plan(plan_file) != plan_document:
            raise BenchmarkError("reviewed plan does not match the frozen benchmark exports")
        account_id, environment = _aws_environment()
        runner = AwsRunner(bucket=bucket, account_id=account_id, base_env=environment)
        aws_cli_version = runner.version()
        remote_uri = runner.uri(plan.prefix)
        fixed_config = workspace / "fixed-aws-config"
        _write_aws_config(fixed_config, ARMS[0])
        _verify_remote_empty(
            runner,
            fixed_config,
            plan.prefix,
            workspace / "initial-empty-check",
            purpose="already contains objects; choose a new run-id",
        )
        claim = _new_claim(run_id, str(plan_document["plan_digest"]))
        _write_claim_exclusive(claim_file, claim)
        reset_dir = workspace / "reset"
        _copy_with_mtimes(
            frozen_before,
            reset_dir,
            future_keys=set(before),
            future_timestamp=time.time() + 86_400,
        )
        remote_claim_created = False
        try:
            _create_remote_claim(runner, plan.prefix, claim_file)
            remote_claim_created = True
            _assert_remote_claim(
                runner,
                plan.prefix,
                claim,
                workspace / "claim-after-create.json",
            )
            sequence = 0
            for round_arms in SCHEDULE:
                for concurrency in round_arms:
                    sequence += 1
                    repetitions[concurrency] += 1
                    trial_dir = workspace / f"trial-{sequence:02d}"
                    trial_dir.mkdir()
                    config = trial_dir / "aws-config"
                    _write_aws_config(config, concurrency)
                    runner.command(
                        fixed_config,
                        "sync",
                        f"{reset_dir}/",
                        remote_uri,
                        "--delete",
                        "--exclude",
                        CLAIM_KEY,
                    )
                    _reset_count, reset_digest = _verify_remote(
                        runner,
                        fixed_config,
                        plan.prefix,
                        trial_dir / "reset-verified",
                        before,
                        claim,
                    )

                    upload_dir = trial_dir / "after"
                    _copy_with_mtimes(
                        frozen_after,
                        upload_dir,
                        future_keys=set(plan.workload.upload_keys),
                        future_timestamp=time.time() + 172_800,
                    )
                    started = time.perf_counter()
                    _upload_serving(runner, config, upload_dir, remote_uri)
                    elapsed = time.perf_counter() - started
                    verified_count, verified_digest = _verify_remote(
                        runner,
                        fixed_config,
                        plan.prefix,
                        trial_dir / "verified",
                        expected,
                        claim,
                    )
                    trials.append(
                        TrialResult(
                            sequence=sequence,
                            repetition=repetitions[concurrency],
                            concurrency=concurrency,
                            elapsed_seconds=elapsed,
                            verified_key_count=verified_count,
                            verified_manifest_digest=verified_digest,
                            reset_manifest_digest=reset_digest,
                            planned_workload_digest=plan.workload.digest,
                        )
                    )
        finally:
            if remote_claim_created:
                _assert_remote_claim(
                    runner,
                    plan.prefix,
                    claim,
                    workspace / "claim-before-cleanup.json",
                )
                runner.command(fixed_config, "rm", remote_uri, "--recursive")
                _verify_remote_empty(
                    runner,
                    fixed_config,
                    plan.prefix,
                    workspace / "cleanup-verified",
                    purpose="cleanup left objects behind",
                )
                cleaned = True

    expected_schedule = tuple(arm for round_arms in SCHEDULE for arm in round_arms)
    if tuple(trial.concurrency for trial in trials) != expected_schedule:
        raise BenchmarkError("benchmark trials do not match the counterbalanced schedule")
    durations = {
        arm: [trial.elapsed_seconds for trial in trials if trial.concurrency == arm] for arm in ARMS
    }
    p50_seconds, threshold, recommended = evaluate_durations(durations)
    report: dict[str, Any] = {
        "schema_version": 1,
        "plan_digest": plan_document["plan_digest"],
        "plan": plan_document["plan"],
        "trials": [asdict(trial) for trial in trials],
        "p50_seconds": p50_seconds,
        "acceptance_threshold_seconds": threshold,
        "recommended_concurrency": recommended,
        "executed_at": datetime.now(UTC).isoformat(),
        "aws_cli_version": aws_cli_version,
        "runner_class": "aws-cli-s3",
        "execution_environment": "github-actions"
        if environment.get("GITHUB_ACTIONS") == "true"
        else "local",
        "claim_digest": hashlib.sha256(
            json.dumps(_claim_document(claim), separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest(),
        "production_config_changed": False,
        "prefix_cleaned": cleaned,
    }
    _write_json(output, report)
    return report


def cleanup_prefix(*, run_id: str, claim_file: Path, bucket: str) -> None:
    prefix = benchmark_prefix(run_id)
    claim = _read_claim(claim_file)
    if claim.run_id != run_id:
        raise BenchmarkError("benchmark claim run-id does not match cleanup run-id")
    account_id, environment = _aws_environment()
    runner = AwsRunner(bucket=bucket, account_id=account_id, base_env=environment)
    with tempfile.TemporaryDirectory(prefix="baibai-serving-upload-cleanup-") as temporary:
        config = Path(temporary) / "aws-config"
        _write_aws_config(config, ARMS[0])
        _assert_remote_claim(
            runner,
            prefix,
            claim,
            Path(temporary) / "remote-claim.json",
        )
        runner.command(config, "rm", runner.uri(prefix), "--recursive")
        _verify_remote_empty(
            runner,
            config,
            prefix,
            Path(temporary) / "cleanup-verified",
            purpose="cleanup left objects behind",
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--before-dir", type=Path, required=True)
    plan_parser.add_argument("--after-dir", type=Path, required=True)
    plan_parser.add_argument("--run-id", required=True)
    plan_parser.add_argument("--output", type=Path)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--before-dir", type=Path, required=True)
    run_parser.add_argument("--after-dir", type=Path, required=True)
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument("--plan", type=Path, required=True)
    run_parser.add_argument("--claim-file", type=Path, required=True)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument(
        "--bucket", default=os.environ.get("R2_SERVING_BUCKET", "baibai-serving")
    )

    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--run-id", required=True)
    cleanup_parser.add_argument("--claim-file", type=Path, required=True)
    cleanup_parser.add_argument(
        "--bucket", default=os.environ.get("R2_SERVING_BUCKET", "baibai-serving")
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "plan":
            create_plan(
                before_dir=args.before_dir,
                after_dir=args.after_dir,
                run_id=args.run_id,
                output=args.output,
            )
        elif args.command == "run":
            run_benchmark(
                before_dir=args.before_dir,
                after_dir=args.after_dir,
                run_id=args.run_id,
                plan_file=args.plan,
                claim_file=args.claim_file,
                output=args.output,
                bucket=args.bucket,
            )
        elif args.command == "cleanup":
            cleanup_prefix(
                run_id=args.run_id,
                claim_file=args.claim_file,
                bucket=args.bucket,
            )
        else:  # pragma: no cover - argparse owns the command choices
            raise AssertionError(args.command)
    except (BenchmarkError, OSError) as exc:
        print(f"serving upload benchmark error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
