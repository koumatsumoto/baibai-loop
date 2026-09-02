"""Produce resumable noncanonical analysis state without becoming domain authority."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import socket
import subprocess  # nosec B404
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import TextIO
from uuid import uuid4

from baibai_batch.analysis.io import (
    digest_json,
    ensure_private_dir,
    read_json,
    redact_argv,
    resolve_executable,
    write_json_atomic,
    write_log,
)
from baibai_batch.jobs.daily import CommandResult, DailyBatchResult, StepResult

_PIPELINE = "daily-analysis"
_SCHEMA_VERSION = 1
_JST = timezone(timedelta(hours=9))


def default_state_dir() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured) if configured else Path.home() / ".local" / "state"
    return base / "baibai-loop"


def _file_digest(path: Path) -> str:
    if not path.is_file():
        return hashlib.sha256(b"").hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repository_fingerprint(root: Path) -> dict[str, str]:
    def git(*args: str) -> str:
        completed = subprocess.run(  # nosec B603
            [resolve_executable("git"), *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain=v1", "--untracked-files=no")
    if status:
        raise ValueError("analysis workspace requires a clean tracked working tree")
    rules = root / "method/screening/rules/current.yaml"
    if not rules.is_file():
        candidates = sorted((root / "method/screening/rules").glob("*.yaml"))
        rules = candidates[-1] if candidates else rules
    return {
        "repo_commit": commit,
        "working_tree_fingerprint": hashlib.sha256(status.encode()).hexdigest(),
        "config_digest": _file_digest(root / "pyproject.toml"),
        "rules_digest": _file_digest(rules),
        "packet_schema_version": "1",
        "prompt_policy_version": (
            "research-triage:" + _file_digest(root / ".agents/skills/research-triage/SKILL.md")
        ),
    }


def _bootstrap_digest(manifest: dict[str, object]) -> str:
    return digest_json(
        {
            key: manifest.get(key)
            for key in (
                "run_id",
                "pipeline",
                "asof",
                "fingerprint",
                "daily_run_revision_id",
                "review_set_id",
                "publication_time",
            )
        }
    )


@dataclass(frozen=True, slots=True)
class Workspace:
    state_root: Path
    path: Path
    manifest_path: Path

    @property
    def run_id(self) -> str:
        value = read_json(self.manifest_path, root=self.state_root)
        if not isinstance(value, dict) or not isinstance(value.get("run_id"), str):
            raise ValueError("workspace manifest has no run_id")
        return str(value["run_id"])

    def manifest(self) -> dict[str, object]:
        value = read_json(self.manifest_path, root=self.state_root)
        if not isinstance(value, dict):
            raise ValueError("workspace manifest must be an object")
        return value

    def update(self, **changes: object) -> dict[str, object]:
        manifest = self.manifest()
        manifest.update(changes)
        manifest["updated_at"] = datetime.now(UTC).isoformat()
        write_json_atomic(self.manifest_path, manifest, root=self.state_root)
        return manifest


class PipelineLock(AbstractContextManager["PipelineLock"]):
    def __init__(self, state_root: Path, asof: str) -> None:
        state_root = ensure_private_dir(state_root)
        lock_dir = ensure_private_dir(state_root / "locks", root=state_root)
        self.path = lock_dir / f"{_PIPELINE}-{asof}.lock"
        self._handle = self.path.open("a+", encoding="utf-8")
        self.path.chmod(0o600)

    def acquire(self) -> bool:
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        now = datetime.now(UTC).isoformat()
        _rewrite_lock_metadata(
            self._handle,
            {
                "schema_version": 1,
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "process_start_identity": _process_start_identity(),
                "acquired_at": now,
                "heartbeat_at": now,
                "workspace": None,
            },
        )
        return True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()


def create_workspace(
    *, state_dir: Path, root: Path, asof: date, force_new: bool = False
) -> Workspace:
    state_root = ensure_private_dir(state_dir)
    asof_root = ensure_private_dir(
        state_root / "runs" / "analysis" / asof.isoformat(), root=state_root
    )
    active_path = asof_root / "active.json"
    fingerprint = repository_fingerprint(root)
    if active_path.is_file() and not force_new:
        active = read_json(active_path, root=state_root)
        if not isinstance(active, dict) or not isinstance(active.get("run_id"), str):
            raise ValueError("active workspace pointer is corrupt")
        existing = asof_root / active["run_id"]
        workspace = Workspace(state_root, existing, existing / "manifest.json")
        current_manifest = workspace.manifest()
        if current_manifest.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("active workspace schema is unsupported; create a new workspace")
        recorded = current_manifest.get("fingerprint")
        if recorded != fingerprint:
            raise ValueError("active workspace fingerprint drift; use --force-new-workspace")
        if active.get("bootstrap_digest") != _bootstrap_digest(current_manifest):
            raise ValueError("active workspace publication identity was modified")
        return workspace
    if active_path.is_file() and force_new:
        active = read_json(active_path, root=state_root)
        previous_id = active.get("run_id") if isinstance(active, dict) else None
        if isinstance(previous_id, str):
            previous_path = asof_root / previous_id
            previous_manifest = previous_path / "manifest.json"
            if previous_manifest.is_file():
                previous = Workspace(state_root, previous_path, previous_manifest)
                previous.update(state="superseded", status="superseded")

    run_id = uuid4().hex
    run_revision_id = f"run-revision-{uuid4().hex}"
    review_set_id = f"review-set-{asof:%Y%m%d}-{uuid4().hex[:12]}"
    path = ensure_private_dir(asof_root / run_id, root=state_root)
    for relative in (
        "inputs",
        "steps",
        "packet/shared",
        "packet/tasks",
        "ai",
        "assembled",
        "publish-intents",
    ):
        ensure_private_dir(path / relative, root=state_root)
    now = datetime.now(UTC).isoformat()
    publication_time = datetime.now(_JST).isoformat()
    manifest: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "run_id": run_id,
        "pipeline": _PIPELINE,
        "asof": asof.isoformat(),
        "created_at": now,
        "updated_at": now,
        "state": "started",
        "status": "started",
        "current_stage": "daily",
        "fingerprint": fingerprint,
        "repo_commit": fingerprint["repo_commit"],
        "working_tree_fingerprint": fingerprint["working_tree_fingerprint"],
        "python_version": platform.python_version(),
        "uv_version": _uv_version(root),
        "config_digest": fingerprint["config_digest"],
        "rules_digest": fingerprint["rules_digest"],
        "packet_schema_version": 1,
        "prompt_policy_version": fingerprint["prompt_policy_version"],
        "daily_run_revision_id": run_revision_id,
        "review_set_id": review_set_id,
        "publication_time": publication_time,
        "macro_context_revision": None,
        "operation_session_id": None,
        "research_triage_expected_head": None,
        "stages": [],
        "ai_task_count": 0,
        "reused_task_count": 0,
        "model_invocation_count": 0,
        "resume_count": 0,
        "failure_reason_code": None,
        "deferred_reason_code": None,
    }
    bootstrap_digest = _bootstrap_digest(manifest)
    manifest["bootstrap_digest"] = bootstrap_digest
    manifest_path = path / "manifest.json"
    write_json_atomic(manifest_path, manifest, root=state_root)
    intent_source = {
        "asof": manifest["asof"],
        "repo_commit": manifest["repo_commit"],
        "config_digest": manifest["config_digest"],
        "rules_digest": manifest["rules_digest"],
        "publication_time": manifest["publication_time"],
    }
    write_json_atomic(
        path / "publish-intents" / "screening-run.json",
        {
            "schema_version": 1,
            "classification": "create-once-idempotent",
            "status": "prepared",
            "target_id": run_revision_id,
            "source_binding": intent_source,
            "source_digest": digest_json(intent_source),
        },
        root=state_root,
    )
    review_source = {**intent_source, "run_revision_id": run_revision_id}
    write_json_atomic(
        path / "publish-intents" / "review-set.json",
        {
            "schema_version": 1,
            "classification": "create-once-idempotent",
            "status": "prepared",
            "target_id": review_set_id,
            "source_binding": review_source,
            "source_digest": digest_json(review_source),
        },
        root=state_root,
    )
    write_json_atomic(
        active_path,
        {
            "schema_version": 1,
            "run_id": run_id,
            "fingerprint": fingerprint,
            "bootstrap_digest": bootstrap_digest,
        },
        root=state_root,
    )
    return Workspace(state_root, path, manifest_path)


def _uv_version(root: Path) -> str:
    completed = subprocess.run(  # nosec B603
        [resolve_executable("uv"), "--version"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() or "unknown"


class StepLogger:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        stages = workspace.manifest().get("stages", [])
        self.results = (
            [dict(stage) for stage in stages if isinstance(stage, dict)]
            if isinstance(stages, list)
            else []
        )
        self.observability_degraded = False
        self.canonical_published = any(
            result.get("name") == "screening-review-set" and result.get("returncode") == 0
            for result in self.results
        )

    def __call__(self, step: StepResult, command: CommandResult) -> None:
        if step.name == "screening-review-set" and command.returncode == 0:
            self.canonical_published = True
        try:
            number = len(self.results) + 1
            directory = ensure_private_dir(
                self.workspace.path / "steps" / f"{number:02d}-{step.name}",
                root=self.workspace.state_root,
            )
            stdout_digest, stdout_truncated = write_log(
                directory / "stdout.log", command.stdout, root=self.workspace.state_root
            )
            stderr_digest, stderr_truncated = write_log(
                directory / "stderr.log", command.stderr, root=self.workspace.state_root
            )
            step_payload = step.to_json()
            argv = step_payload.get("argv")
            if isinstance(argv, list):
                step_payload["argv"] = redact_argv(argv)
            meta = step_payload | {
                "attempt": 1 + sum(existing.get("name") == step.name for existing in self.results),
                "stdout_path": str((directory / "stdout.log").relative_to(self.workspace.path)),
                "stderr_path": str((directory / "stderr.log").relative_to(self.workspace.path)),
                "stdout_digest": stdout_digest,
                "stderr_digest": stderr_digest,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            }
            write_json_atomic(directory / "meta.json", meta, root=self.workspace.state_root)
            self.results.append(meta)
            self.workspace.update(stages=list(self.results), current_stage=step.name)
            _heartbeat_lock(self.workspace)
        except (OSError, ValueError):
            if not self.canonical_published:
                raise
            self.observability_degraded = True


def _process_start_identity() -> str:
    try:
        return Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8").split()[21]
    except (OSError, IndexError):
        return "unknown"


def _rewrite_lock_metadata(handle: TextIO, payload: dict[str, object]) -> None:
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def _heartbeat_lock(workspace: Workspace) -> None:
    path = workspace.state_root / "locks" / f"{_PIPELINE}-{workspace.manifest()['asof']}.lock"
    if not path.is_file() or path.is_symlink():
        return
    with path.open("r+", encoding="utf-8") as handle:
        try:
            payload = json.load(handle)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or payload.get("pid") != os.getpid():
            return
        payload["heartbeat_at"] = datetime.now(UTC).isoformat()
        payload["workspace"] = str(workspace.path)
        _rewrite_lock_metadata(handle, payload)


def record_daily_result(workspace: Workspace, result: DailyBatchResult) -> None:
    daily_manifest = result.to_json()
    path = workspace.path / "inputs" / "daily-manifest.json"
    write_json_atomic(path, daily_manifest, root=workspace.state_root)
    for name, target_id in (
        ("screening-run", result.run_revision_id),
        ("review-set", result.review_set_id),
    ):
        intent_path = workspace.path / "publish-intents" / f"{name}.json"
        if target_id is not None and intent_path.is_file():
            intent = read_json(intent_path, root=workspace.state_root)
            if not isinstance(intent, dict) or intent.get("target_id") != target_id:
                raise ValueError(f"{name} publish intent differs from canonical result")
            write_json_atomic(
                intent_path,
                {**intent, "status": "confirmed"},
                root=workspace.state_root,
            )
    changes: dict[str, object] = {
        "state": "machine_complete",
        "status": result.status,
        "current_stage": "prepare",
        "deferred_reason_code": ("daily_deferred" if result.deferred_failure_count else None),
        "business_exit_code": result.exit_code,
        "deferred_failure_count": result.deferred_failure_count,
        "input_artifacts": [
            {
                "path": str(path.relative_to(workspace.path)),
                "sha256": digest_json(daily_manifest),
            }
        ],
    }
    if result.run_revision_id is not None:
        changes["daily_run_revision_id"] = result.run_revision_id
    if result.review_set_id is not None:
        changes["review_set_id"] = result.review_set_id
    workspace.update(**changes)


def rebind_bootstrap(
    workspace: Workspace, *, run_revision_id: object, review_set_id: object
) -> None:
    manifest = workspace.manifest()
    if manifest.get("state") != "started" or manifest.get("stages"):
        raise ValueError("publication identity can only be bound before workspace execution")
    manifest["daily_run_revision_id"] = run_revision_id
    manifest["review_set_id"] = review_set_id
    bootstrap_digest = _bootstrap_digest(manifest)
    manifest["bootstrap_digest"] = bootstrap_digest
    write_json_atomic(workspace.manifest_path, manifest, root=workspace.state_root)
    active_path = workspace.path.parent / "active.json"
    write_json_atomic(
        active_path,
        {
            "schema_version": 1,
            "run_id": workspace.run_id,
            "fingerprint": manifest["fingerprint"],
            "bootstrap_digest": bootstrap_digest,
        },
        root=workspace.state_root,
    )
    for name, target_id in (
        ("screening-run", run_revision_id),
        ("review-set", review_set_id),
    ):
        if target_id is None:
            continue
        intent_path = workspace.path / "publish-intents" / f"{name}.json"
        intent = read_json(intent_path, root=workspace.state_root)
        if not isinstance(intent, dict):
            raise ValueError(f"{name} publish intent is invalid")
        intent["target_id"] = target_id
        intent["status"] = "confirmed_external_manifest"
        if name == "review-set":
            source = intent.get("source_binding")
            if isinstance(source, dict):
                source["run_revision_id"] = run_revision_id
                intent["source_digest"] = digest_json(source)
        write_json_atomic(intent_path, intent, root=workspace.state_root)


def resolve_workspace(path: Path) -> Workspace:
    resolved = path.resolve(strict=True)
    manifest_path = resolved / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"workspace manifest is unavailable: {manifest_path}")
    asof_root = resolved.parent
    analysis_root = asof_root.parent
    runs_root = analysis_root.parent
    if analysis_root.name != "analysis" or runs_root.name != "runs":
        raise ValueError("workspace is not below <state>/runs/analysis/<asof>")
    if date.fromisoformat(asof_root.name).isoformat() != asof_root.name:
        raise ValueError("workspace parent is not an ISO asof date")
    state_root = runs_root.parent
    return Workspace(state_root, resolved, manifest_path)


def validate_workspace(
    workspace: Workspace, *, root: Path, require_supported_schema: bool = True
) -> None:
    manifest = workspace.manifest()
    if require_supported_schema and manifest.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("workspace schema is unsupported for resume")
    if manifest.get("fingerprint") != repository_fingerprint(root):
        raise ValueError("workspace fingerprint differs from the current repository")
    active_path = workspace.path.parent / "active.json"
    active = read_json(active_path, root=workspace.state_root)
    if not isinstance(active, dict) or active.get("run_id") != workspace.run_id:
        raise ValueError("workspace is not the exact active run for its asof")
    if active.get("bootstrap_digest") != _bootstrap_digest(manifest):
        raise ValueError("workspace publication identity differs from its active pointer")


__all__ = [
    "PipelineLock",
    "StepLogger",
    "Workspace",
    "create_workspace",
    "default_state_dir",
    "rebind_bootstrap",
    "record_daily_result",
    "repository_fingerprint",
    "resolve_workspace",
    "validate_workspace",
]
