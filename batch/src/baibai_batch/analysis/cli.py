"""Expose one deterministic entrypoint for the noncanonical analysis workspace."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess  # nosec B404
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import FrameType

import yaml

from baibai_batch.analysis.io import (
    digest_json,
    read_json,
    read_text_bounded,
    redact,
    resolve_executable,
    write_json_atomic,
)
from baibai_batch.analysis.models import DailyManifest, PacketIndex
from baibai_batch.analysis.packet import check_results, prepare_packet, validate_packet
from baibai_batch.analysis.workspace import (
    PipelineLock,
    StepLogger,
    Workspace,
    create_workspace,
    default_state_dir,
    rebind_bootstrap,
    record_daily_result,
    resolve_workspace,
    validate_workspace,
)
from baibai_batch.jobs.daily import (
    BatchStepError,
    CalendarCoverageError,
    _run_subprocess,
    run_daily_batch_structured,
)


def _summary(workspace: Workspace, *, status: str | None = None) -> dict[str, object]:
    manifest = workspace.manifest()
    index_path = workspace.path / "packet" / "index.json"
    index = read_json(index_path, root=workspace.state_root) if index_path.is_file() else {}
    tasks: object = index.get("tasks", []) if isinstance(index, dict) else []
    return {
        "status": status or manifest.get("status"),
        "asof": manifest.get("asof"),
        "run_id": manifest.get("run_id"),
        "ai_tasks": manifest.get("ai_task_count", 0),
        "reused_tasks": manifest.get("reused_task_count", 0),
        "task_types": _task_counts(tasks),
        "workspace": str(workspace.path),
        "manifest": str(workspace.manifest_path),
        "log_dir": str(workspace.path / "steps"),
    }


def _task_counts(tasks: object) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(tasks, list):
        return counts
    for task in tasks:
        if isinstance(task, dict) and isinstance(task.get("type"), str):
            counts[task["type"]] = counts.get(task["type"], 0) + 1
    return counts


def _business_exit(workspace: Workspace, successful_exit: int = 0) -> int:
    """Preserve the daily job's degraded-success notification contract."""

    if successful_exit != 0:
        return successful_exit
    manifest = workspace.manifest()
    exit_code = manifest.get("business_exit_code", 0)
    return exit_code if exit_code == 3 else 0


def _daily_resume_stage(logger: StepLogger) -> str | None:
    successful = {
        result.get("name") for result in logger.results if result.get("returncode") in {0, 2}
    }
    if "screening-review-set" in successful:
        return "screening-review-set"
    if "screening-run" in successful:
        return "screening-run"
    return None


def _show_bound_review_set(root: Path, manifest: dict[str, object]) -> dict[str, object]:
    review_set_id = manifest.get("review_set_id")
    if not isinstance(review_set_id, str) or not review_set_id:
        raise ValueError("workspace has no bound Review Set identity")
    completed = subprocess.run(  # nosec B603
        [
            resolve_executable("baibai-engine"),
            "screening",
            "review-set",
            "show",
            "--review-set-id",
            review_set_id,
            "--format",
            "json",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("bound Review Set could not be reconciled for daily resume")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict) or value.get("review_set_id") != review_set_id:
        raise ValueError("bound Review Set reconciliation returned a different identity")
    return value


def _emit(payload: dict[str, object], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    for key in (
        "status",
        "asof",
        "run_id",
        "ai_tasks",
        "reused_tasks",
        "workspace",
        "manifest",
        "log_dir",
    ):
        if key in payload:
            print(f"{key}={payload[key]}")


def _start(args: argparse.Namespace) -> int:
    asof = date.fromisoformat(args.asof)
    state_dir = args.state_dir.absolute()
    lock = PipelineLock(state_dir, asof.isoformat())
    with lock:
        if not lock.acquire():
            _emit({"status": "already_running", "asof": asof.isoformat()}, args.format)
            return 0
        workspace = create_workspace(
            state_dir=state_dir,
            root=args.repo_root,
            asof=asof,
            force_new=args.force_new_workspace,
        )

        def interrupted(signum: int, _frame: FrameType | None) -> None:
            workspace.update(
                state="interrupted",
                status="interrupted",
                failure_reason_code=f"signal:{signum}",
            )
            raise InterruptedError(f"analysis interrupted by signal {signum}")

        signal.signal(signal.SIGINT, interrupted)
        signal.signal(signal.SIGTERM, interrupted)
        manifest = workspace.manifest()
        if manifest.get("state") == "interrupted":
            resume_count = manifest.get("resume_count", 0)
            if not isinstance(resume_count, int) or isinstance(resume_count, bool):
                raise ValueError("workspace resume_count must be an integer")
            workspace.update(resume_count=resume_count + 1)
            manifest = workspace.manifest()
        if manifest.get("state") in {"published", "awaiting_human", "no_ai"}:
            _emit(_summary(workspace, status="already_complete"), args.format)
            return _business_exit(workspace)
        if manifest.get("state") in {"ai_required", "machine_incomplete"}:
            _emit(_summary(workspace), args.format)
            return 1 if manifest.get("state") == "machine_incomplete" else _business_exit(workspace)
        if manifest.get("state") == "checked":
            return _publish_locked(args, workspace)
        if manifest.get("state") == "failed":
            _emit(_summary(workspace), args.format)
            return 1
        daily_manifest = workspace.path / "inputs" / "daily-manifest.json"
        if daily_manifest.is_file():
            result_status = manifest.get("status")
            if result_status == "deferred":
                _emit(_summary(workspace), args.format)
                return 3
        else:
            logger = StepLogger(workspace)
            resume_after_stage = _daily_resume_stage(logger)
            if resume_after_stage == "screening-review-set":
                publication = _show_bound_review_set(args.repo_root, manifest)
                if publication.get("run_revision_id") != manifest.get("daily_run_revision_id"):
                    raise ValueError("published Review Set differs from the bound run revision")
                if publication.get("as_of") != manifest.get("asof"):
                    raise ValueError("published Review Set differs from the workspace asof")
            try:
                result = run_daily_batch_structured(
                    root=args.repo_root,
                    output_dir=workspace.path / "assembled" / "serving",
                    asof=asof,
                    runner=_run_subprocess,
                    quiet=not args.verbose,
                    step_sink=logger,
                    run_revision_id=str(manifest["daily_run_revision_id"]),
                    review_set_id=str(manifest["review_set_id"]),
                    publication_time=datetime.fromisoformat(str(manifest["publication_time"])),
                    gate_explicit_asof=True,
                    resume_after_stage=resume_after_stage,
                )
            except (BatchStepError, CalendarCoverageError) as exc:
                workspace.update(
                    state="failed",
                    status="failed",
                    failure_reason_code=f"daily:{exc.stage or 'batch'}",
                )
                _record_failure(
                    workspace,
                    stage=exc.stage or "daily",
                    reason_code=f"daily:{exc.stage or 'batch'}",
                    error=exc,
                )
                raise
            try:
                record_daily_result(workspace, result)
            except (OSError, ValueError) as exc:
                if not logger.canonical_published:
                    raise
                print(
                    f"observability_degraded: canonical Review Set is published but "
                    f"workspace finalization failed: {redact(str(exc))[-4000:]}",
                    file=sys.stderr,
                )
                _emit(
                    {
                        "status": "observability_degraded",
                        "asof": asof.isoformat(),
                        "workspace": str(workspace.path),
                    },
                    args.format,
                )
                return result.exit_code
            if logger.observability_degraded:
                workspace.update(observability_degraded=True)
            if result.exit_code not in (0, 3):
                return result.exit_code
        try:
            index = prepare_packet(
                workspace,
                root=args.repo_root,
                macro_review=args.macro_review,
                re_evaluate=args.re_evaluate,
            )
        except (OSError, ValueError) as exc:
            workspace.update(state="failed", status="failed", failure_reason_code="packet")
            _record_failure(workspace, stage="packet", reason_code="packet", error=exc)
            raise
        machine_exit = _publish_reused_tasks(workspace, index=index, args=args)
        if machine_exit is not None:
            return _business_exit(workspace, machine_exit)
        _write_metrics(workspace)
        _emit(_summary(workspace), args.format)
        if index.status == "machine_incomplete":
            return 1
        return _business_exit(workspace)


def _prepare(args: argparse.Namespace) -> int:
    if args.daily_manifest.is_symlink() or not args.daily_manifest.is_file():
        raise ValueError("daily manifest must be a regular non-symlink file")
    if args.daily_manifest.stat().st_size > 2_000_000:
        raise ValueError("daily manifest exceeds the 2000000 byte input limit")
    value = DailyManifest.model_validate_json(
        args.daily_manifest.read_text(encoding="utf-8")
    ).model_dump(mode="json")
    asof = date.fromisoformat(value["asof"])
    state_dir = args.state_dir.absolute()
    lock = PipelineLock(state_dir, asof.isoformat())
    with lock:
        if not lock.acquire():
            _emit({"status": "already_running", "asof": asof.isoformat()}, args.format)
            return 0
        workspace = create_workspace(
            state_dir=state_dir,
            root=args.repo_root,
            asof=asof,
            force_new=args.force_new_workspace,
        )
        target = workspace.path / "inputs" / "daily-manifest.json"
        manifest = workspace.manifest()
        if manifest.get("state") == "started":
            write_json_atomic(target, value, root=workspace.state_root)
            rebind_bootstrap(
                workspace,
                run_revision_id=value.get("run_revision_id"),
                review_set_id=value.get("review_set_id"),
            )
            workspace.update(
                state="machine_complete",
                status=str(value.get("status", "machine_complete")),
                current_stage="prepare",
                daily_run_revision_id=value.get("run_revision_id"),
                review_set_id=value.get("review_set_id"),
                business_exit_code=value.get("exit_code", 0),
                deferred_failure_count=value.get("deferred_failure_count", 0),
                deferred_reason_code=(
                    "daily_deferred" if value.get("deferred_failure_count") else None
                ),
            )
        else:
            existing = read_json(target, root=workspace.state_root)
            if digest_json(existing) != digest_json(value):
                raise ValueError("daily manifest differs from the exact active workspace input")
            if manifest.get("state") in {"no_ai", "published", "awaiting_human"}:
                _emit(_summary(workspace, status="already_complete"), args.format)
                return _business_exit(workspace)
            if manifest.get("state") in {"ai_required", "machine_incomplete", "checked"}:
                if manifest.get("state") == "checked":
                    return _publish_locked(args, workspace)
                _emit(_summary(workspace), args.format)
                return (
                    1
                    if manifest.get("state") == "machine_incomplete"
                    else _business_exit(workspace)
                )
            if manifest.get("state") == "failed":
                _emit(_summary(workspace), args.format)
                return 1
        try:
            index = prepare_packet(
                workspace,
                root=args.repo_root,
                macro_review=args.macro_review,
                re_evaluate=args.re_evaluate,
            )
        except (OSError, ValueError) as exc:
            workspace.update(state="failed", status="failed", failure_reason_code="packet")
            _record_failure(workspace, stage="packet", reason_code="packet", error=exc)
            raise
        machine_exit = _publish_reused_tasks(workspace, index=index, args=args)
        if machine_exit is not None:
            return _business_exit(workspace, machine_exit)
        _write_metrics(workspace)
        _emit(_summary(workspace), args.format)
        if index.status == "machine_incomplete":
            return 1
        return _business_exit(workspace)


def _status(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args.workspace)
    validate_workspace(workspace, root=args.repo_root, require_supported_schema=False)
    if workspace.manifest().get("schema_version") != 1:
        _emit(_summary(workspace, status="unsupported_workspace_schema"), args.format)
        return 0
    if (workspace.path / "packet" / "index.json").is_file():
        validate_packet(workspace)
    _emit(_summary(workspace), args.format)
    return 0


def _check(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args.workspace)
    lock = PipelineLock(workspace.state_root, str(workspace.manifest()["asof"]))
    with lock:
        if not lock.acquire():
            _emit(_summary(workspace, status="already_running"), args.format)
            return 0
        validate_workspace(workspace, root=args.repo_root)
        state = workspace.manifest().get("state")
        if state in {"published", "awaiting_human"}:
            _emit(_summary(workspace, status="already_complete"), args.format)
            return _business_exit(workspace)
        if state == "checked":
            _emit(_summary(workspace, status="checked"), args.format)
            return _business_exit(workspace)
        if state != "ai_required":
            raise ValueError("workspace must be ai_required before analysis check")
        status, draft = check_results(workspace, results_path=args.ai_results, root=args.repo_root)
        payload = _summary(workspace, status=status)
        if draft is not None:
            payload["assembled_draft"] = str(draft)
        _write_metrics(workspace)
        _emit(payload, args.format)
        return _business_exit(workspace)


def _publish(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args.workspace)
    lock = PipelineLock(workspace.state_root, str(workspace.manifest()["asof"]))
    with lock:
        if not lock.acquire():
            _emit(_summary(workspace, status="already_running"), args.format)
            return 0
        return _publish_locked(args, workspace)


def _publish_locked(args: argparse.Namespace, workspace: Workspace) -> int:
    validate_workspace(workspace, root=args.repo_root)
    manifest = workspace.manifest()
    if manifest.get("state") in {"published", "awaiting_human"}:
        _emit(_summary(workspace, status="already_complete"), args.format)
        return _business_exit(workspace)
    if manifest.get("state") != "checked":
        raise ValueError("workspace must pass analysis check before publish")
    draft = workspace.path / "assembled" / "research-triage.yaml"
    draft_value = yaml.safe_load(draft.read_text(encoding="utf-8"))
    entries = draft_value.get("entries", []) if isinstance(draft_value, dict) else []
    research_count = sum(
        isinstance(entry, dict) and entry.get("decision") == "research" for entry in entries
    )
    triage_id = draft_value.get("research_triage_id") if isinstance(draft_value, dict) else None
    if not isinstance(triage_id, str) or not triage_id:
        raise ValueError("assembled Research Triage has no publication identity")
    draft_digest = digest_json(draft_value)
    intent_path = workspace.path / "publish-intents" / "research-triage.json"
    intent = {
        "schema_version": 1,
        "classification": "create-once-idempotent-cas",
        "status": "prepared",
        "target_id": triage_id,
        "content_digest": draft_digest,
        "review_set_id": manifest.get("review_set_id"),
        "expected_head": manifest.get("research_triage_expected_head"),
    }
    if intent_path.is_file():
        existing_intent = read_json(intent_path, root=workspace.state_root)
        if existing_intent not in (intent, {**intent, "status": "confirmed"}):
            raise ValueError("Research Triage publish intent differs on resume")
    else:
        write_json_atomic(intent_path, intent, root=workspace.state_root)
    completed = subprocess.run(  # nosec B603
        [
            resolve_executable("baibai-engine"),
            "screening",
            "research-triage",
            "publish",
            str(draft),
        ],
        cwd=args.repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        workspace.update(state="failed", status="failed", failure_reason_code="triage_publish")
        _record_failure(
            workspace,
            stage="triage-publish",
            reason_code="triage_publish",
            error=ValueError(completed.stderr.rstrip() or "publisher returned a non-zero exit"),
        )
        print(redact(completed.stderr.rstrip())[-4000:], file=sys.stderr)
        return completed.returncode
    if research_count == 0 and manifest.get("macro_phase") != "independent_complete":
        _complete_no_research_operation(
            args.repo_root,
            workspace,
            operation_id=manifest.get("operation_session_id"),
            research_triage_id=triage_id,
        )
    status = (
        "awaiting_human"
        if research_count or manifest.get("macro_phase") == "independent_complete"
        else "published"
    )
    try:
        write_json_atomic(intent_path, {**intent, "status": "confirmed"}, root=workspace.state_root)
        workspace.update(state=status, status=status, current_stage="complete")
        _write_metrics(workspace)
    except (OSError, ValueError) as exc:
        print(
            f"observability_degraded: canonical Research Triage is published but "
            f"workspace finalization failed: {redact(str(exc))[-4000:]}",
            file=sys.stderr,
        )
        _emit({"status": "observability_degraded", "workspace": str(workspace.path)}, args.format)
        return _business_exit(workspace)
    _emit(_summary(workspace), args.format)
    return _business_exit(workspace)


def _complete_no_research_operation(
    root: Path,
    workspace: Workspace,
    *,
    operation_id: object,
    research_triage_id: str,
) -> None:
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("zero-research completion requires the bound operation session")
    payload = {
        "checkpoint": "research_triage cycle complete",
        "artifacts": [
            {
                "kind": "research_triage",
                "ref": research_triage_id,
                "research_count": 0,
            }
        ],
        "canonical_refs": [research_triage_id],
        "human_confirmation": None,
        "completion_reason": "no-research",
        "result": "no candidate was admitted to the Research Set",
        "next": "wait for the next capital-allocation trigger",
    }
    operation = _operation_show(root, operation_id)
    if operation.get("status") == "completed":
        if operation.get("payload") != payload:
            raise ValueError("completed operation payload differs from zero-research intent")
        return
    if operation.get("status") != "active":
        raise ValueError("bound operation is neither active nor completed")
    payload_path = workspace.path / "assembled" / "operation-no-research.json"
    write_json_atomic(payload_path, payload, root=workspace.state_root)
    completed = subprocess.run(  # nosec B603
        [
            resolve_executable("baibai-engine"),
            "operation",
            "--format",
            "json",
            "complete",
            operation_id,
            "--payload",
            str(payload_path),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("zero-research operation completion failed")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("operation completion did not return JSON") from exc
    if (
        not isinstance(result, dict)
        or result.get("operation_id") != operation_id
        or result.get("status") != "completed"
        or result.get("payload") != payload
    ):
        raise ValueError("operation completion returned a different canonical result")


def _operation_show(root: Path, operation_id: str) -> dict[str, object]:
    completed = subprocess.run(  # nosec B603
        [
            resolve_executable("baibai-engine"),
            "operation",
            "--format",
            "json",
            "show",
            operation_id,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("bound operation could not be reconciled")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("operation reconciliation did not return JSON") from exc
    if not isinstance(value, dict) or value.get("operation_id") != operation_id:
        raise ValueError("operation reconciliation returned a different identity")
    return value


def _publish_reused_tasks(
    workspace: Workspace, *, index: PacketIndex, args: argparse.Namespace
) -> int | None:
    if (
        index.status != "no_ai"
        or not index.tasks
        or any(task.type != "research-triage" or not task.reused for task in index.tasks)
    ):
        return None
    results = workspace.path / "ai/results.json"
    write_json_atomic(
        results,
        {"schema_version": 1, "packet_id": index.packet_id, "results": []},
        root=workspace.state_root,
    )
    status, _draft = check_results(workspace, results_path=results, root=args.repo_root)
    if status != "checked":
        raise ValueError("reused Research Triage tasks did not assemble to checked state")
    return _publish_locked(args, workspace)


def _logs(args: argparse.Namespace) -> int:
    workspace = resolve_workspace(args.workspace)
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", args.stage) is None:
        raise ValueError("stage must contain only lowercase letters, digits, and hyphens")
    if args.tail < 0 or args.tail > 10_000:
        raise ValueError("tail must be between 0 and 10000")
    candidates = sorted((workspace.path / "steps").glob(f"*-{args.stage}"))
    if not candidates:
        raise ValueError(f"stage log is unavailable: {args.stage}")
    selected = candidates[-1]
    for stream in ("stdout.log", "stderr.log"):
        lines = read_text_bounded(selected / stream, root=workspace.state_root).splitlines()
        print(f"[{stream}]")
        print("\n".join(lines[-args.tail :]))
    return 0


def _prune(args: argparse.Namespace) -> int:
    if args.older_than_days < 0 or args.failed_older_than_days < 0:
        raise ValueError("retention days must be zero or greater")
    state_dir = args.state_dir.resolve(strict=True)
    cutoff = datetime.now(UTC) - timedelta(days=args.older_than_days)
    failed_cutoff = datetime.now(UTC) - timedelta(days=args.failed_older_than_days)
    removed = 0
    for asof_dir in sorted((state_dir / "runs" / "analysis").glob("????-??-??")):
        lock = PipelineLock(state_dir, asof_dir.name)
        with lock:
            if not lock.acquire():
                continue
            active_id: str | None = None
            active_path = asof_dir / "active.json"
            if active_path.is_file():
                active = read_json(active_path, root=state_dir)
                active_id = active.get("run_id") if isinstance(active, dict) else None
            for workspace_path in sorted(
                path for path in asof_dir.iterdir() if path.is_dir() and not path.is_symlink()
            ):
                manifest_path = workspace_path / "manifest.json"
                if not manifest_path.is_file():
                    continue
                manifest = read_json(manifest_path, root=state_dir)
                if not isinstance(manifest, dict) or not isinstance(
                    manifest.get("updated_at"), str
                ):
                    continue
                state = manifest.get("state")
                if workspace_path.name == active_id:
                    continue
                relevant_cutoff = (
                    failed_cutoff
                    if state
                    in {
                        "failed",
                        "started",
                        "machine_complete",
                        "ai_required",
                        "checked",
                        "interrupted",
                    }
                    else cutoff
                )
                if datetime.fromisoformat(manifest["updated_at"]) >= relevant_cutoff:
                    continue
                shutil.rmtree(workspace_path)
                if workspace_path.name == active_id:
                    active_path.unlink(missing_ok=True)
                removed += 1
    _emit({"status": "pruned", "removed": removed}, args.format)
    return 0


def _write_metrics(workspace: Workspace) -> None:
    manifest = workspace.manifest()
    stages = manifest.get("stages", [])
    machine_time = 0.0
    log_bytes = 0
    stdout_bytes = 0
    retry_count = 0
    if isinstance(stages, list):
        for stage in stages:
            if isinstance(stage, dict):
                value = stage.get("duration_seconds")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    machine_time += float(value)
                attempt = stage.get("attempt")
                if isinstance(attempt, int) and attempt > 1:
                    retry_count += 1
        log_bytes = sum(path.stat().st_size for path in (workspace.path / "steps").rglob("*.log"))
        stdout_bytes = sum(
            path.stat().st_size for path in (workspace.path / "steps").rglob("stdout.log")
        )
    index_path = workspace.path / "packet" / "index.json"
    packet = read_json(index_path, root=workspace.state_root) if index_path.is_file() else {}
    metrics = {
        "schema_version": 1,
        "machine_commands": len(stages) if isinstance(stages, list) else 0,
        "model_invocation_count": manifest.get("model_invocation_count", 0),
        "ai_turns": manifest.get("model_invocation_count", 0),
        "task_total": (len(packet.get("tasks", [])) if isinstance(packet, dict) else 0),
        "reused_tasks": manifest.get("reused_task_count", 0),
        "newly_evaluated": manifest.get("ai_task_count", 0),
        "packet_bytes": packet.get("packet_bytes", 0) if isinstance(packet, dict) else 0,
        "packet_index_bytes": index_path.stat().st_size if index_path.is_file() else 0,
        "task_bytes": packet.get("packet_bytes", 0) if isinstance(packet, dict) else 0,
        "estimated_input_tokens": packet.get("estimated_tokens", 0)
        if isinstance(packet, dict)
        else 0,
        "actual_input_tokens": None,
        "actual_output_tokens": None,
        "wall_clock_seconds": round(
            (
                datetime.now(UTC) - datetime.fromisoformat(str(manifest["created_at"]))
            ).total_seconds(),
            6,
        ),
        "machine_time_seconds": round(machine_time, 6),
        "ai_time_seconds": None,
        "stdout_bytes": stdout_bytes,
        "log_bytes": log_bytes,
        "retry_count": retry_count,
        "resume_count": manifest.get("resume_count", 0),
        "final_status": manifest.get("status"),
    }
    write_json_atomic(workspace.path / "metrics.json", metrics, root=workspace.state_root)


def _record_failure(
    workspace: Workspace, *, stage: str, reason_code: str, error: BaseException
) -> None:
    write_json_atomic(
        workspace.path / "failure_packet.json",
        {
            "schema_version": 1,
            "status": "failed",
            "stage": stage,
            "reason_code": reason_code,
            "hint": redact(str(error))[-4000:],
            "log_dir": "steps",
        },
        root=workspace.state_root,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-batch analysis")
    commands = parser.add_subparsers(dest="analysis_command", required=True)

    start = commands.add_parser("start")
    start.add_argument("--asof", required=True)
    _state_args(start)
    start.add_argument("--macro-review", action="store_true")
    start.add_argument("--re-evaluate", action="store_true")
    start.add_argument(
        "--verbose", action="store_true", help="mirror step progress while retaining full logs"
    )

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--daily-manifest", type=Path, required=True)
    _state_args(prepare)
    prepare.add_argument("--macro-review", action="store_true")
    prepare.add_argument("--re-evaluate", action="store_true")

    status = commands.add_parser("status")
    status.add_argument("--workspace", type=Path, required=True)
    status.add_argument("--repo-root", type=Path, default=Path.cwd())
    status.add_argument("--format", choices=("text", "json"), default="text")

    check = commands.add_parser("check")
    check.add_argument("--workspace", type=Path, required=True)
    check.add_argument("--ai-results", type=Path, required=True)
    check.add_argument("--repo-root", type=Path, default=Path.cwd())
    check.add_argument("--format", choices=("text", "json"), default="text")

    publish = commands.add_parser("publish")
    publish.add_argument("--workspace", type=Path, required=True)
    publish.add_argument("--repo-root", type=Path, default=Path.cwd())
    publish.add_argument("--format", choices=("text", "json"), default="text")

    logs = commands.add_parser("logs")
    logs.add_argument("--workspace", type=Path, required=True)
    logs.add_argument("--stage", required=True)
    logs.add_argument("--tail", type=int, default=100)

    prune = commands.add_parser("prune-runs")
    prune.add_argument("--state-dir", type=Path, default=default_state_dir())
    prune.add_argument("--older-than-days", type=int, required=True)
    prune.add_argument(
        "--failed-older-than-days",
        type=int,
        default=60,
        help="retain failed runs longer than successful runs",
    )
    prune.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def _state_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--force-new-workspace", action="store_true")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(args, "repo_root"):
        args.repo_root = args.repo_root.resolve()
    previous_umask = os.umask(0o077)
    try:
        try:
            return {
                "start": _start,
                "prepare": _prepare,
                "status": _status,
                "check": _check,
                "publish": _publish,
                "logs": _logs,
                "prune-runs": _prune,
            }[args.analysis_command](args)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {redact(str(exc))[-4000:]}", file=sys.stderr)
            return 1
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
