"""Measure judgment inputs and expose only bounded, schema-owned AI tasks."""

from __future__ import annotations

import hashlib
import json
import subprocess  # nosec B404
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml

from baibai_batch.analysis.io import (
    canonical_json,
    digest_json,
    ensure_private_dir,
    read_json,
    resolve_executable,
    write_bytes_atomic,
    write_json_atomic,
)
from baibai_batch.analysis.models import (
    AIResult,
    AIResultEnvelope,
    BatchReference,
    MacroJudgment,
    PacketIndex,
    TaskReference,
    TriageJudgment,
)
from baibai_batch.analysis.workspace import Workspace

_POLICY_VERSION = "analysis-policy-v1"
_TRIAGE_SCHEMA = "research-triage-result-v1"
_TRIAGE_SCHEMA_DIGEST = digest_json(TriageJudgment.model_json_schema())
_MACRO_SCHEMA = "macro-independent-result-v1"
_TASK_MAX_BYTES = 96_000
_INDEX_MAX_BYTES = 64_000
_RESULT_MAX_BYTES = 512_000
_SHARED_MAX_BYTES = 512_000
_PACKET_MAX_BYTES = 2_000_000
_BATCH_MAX_BYTES = 512_000
_JST = ZoneInfo("Asia/Tokyo")


class _OperationAsOfConflict(ValueError):
    """Keep a prior human Triage gate while allowing an independent Macro trigger."""


def _source_binding(manifest: dict[str, object]) -> dict[str, object]:
    keys = (
        "run_id",
        "pipeline",
        "asof",
        "fingerprint",
        "config_digest",
        "rules_digest",
        "packet_schema_version",
        "prompt_policy_version",
        "daily_run_revision_id",
        "review_set_id",
        "publication_time",
        "bootstrap_digest",
        "macro_context_revision",
        "operation_session_id",
        "research_triage_expected_head",
        "triage_scaffold_sha256",
    )
    return {key: manifest.get(key) for key in keys}


def _run_json(argv: list[str], *, root: Path) -> object:
    resolved_argv = [resolve_executable(argv[0]), *argv[1:]]
    completed = subprocess.run(  # nosec B603
        resolved_argv, cwd=root, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise ValueError(f"machine command failed ({completed.returncode}): {' '.join(argv[:4])}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"machine command did not return one JSON value: {argv[:4]}") from exc


def _bind_operation(root: Path, asof: str) -> str:
    active = _run_json(
        ["baibai-engine", "operation", "--format", "json", "show", "--status", "active"],
        root=root,
    )
    if not isinstance(active, dict) or not isinstance(active.get("operations"), list):
        raise ValueError("operation show returned an invalid active-session view")
    operations = active["operations"]
    if len(operations) > 1:
        raise ValueError("operation store violates the one-active-session contract")
    if operations:
        operation = operations[0]
        if not isinstance(operation, dict) or operation.get("session_kind") != "capital-allocation":
            raise ValueError("an active non-capital-allocation operation blocks daily analysis")
        if operation.get("as_of") != asof:
            raise _OperationAsOfConflict(
                "active capital-allocation operation belongs to a different asof"
            )
    else:
        operation = _run_json(
            [
                "baibai-engine",
                "operation",
                "--format",
                "json",
                "start",
                "--kind",
                "capital-allocation",
                "--as-of",
                asof,
            ],
            root=root,
        )
    operation_id = operation.get("operation_id") if isinstance(operation, dict) else None
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("operation session has no operation_id")
    return operation_id


def _review_set(root: Path, review_set_id: str) -> dict[str, object]:
    value = _run_json(
        [
            "baibai-engine",
            "screening",
            "review-set",
            "show",
            "--review-set-id",
            review_set_id,
            "--format",
            "json",
        ],
        root=root,
    )
    if not isinstance(value, dict) or value.get("review_set_id") != review_set_id:
        raise ValueError("Review Set JSON does not match the manifest binding")
    return value


def _scaffold(root: Path, workspace: Workspace, review_set_id: str) -> dict[str, object]:
    path = workspace.path / "assembled" / "research-triage-scaffold.yaml"
    completed = subprocess.run(  # nosec B603
        [
            resolve_executable("baibai-engine"),
            "screening",
            "research-triage",
            "scaffold",
            "--review-set-id",
            review_set_id,
            "--output-path",
            str(path),
            "--force",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("research-triage scaffold failed")
    path.chmod(0o600)
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("review_set_id") != review_set_id:
        raise ValueError("research-triage scaffold does not match the Review Set")
    return value


def _task_payload(
    *,
    entry: dict[str, object],
    macro_context_id: object,
    rules_digest: str,
    prompt_policy_version: str,
    macro_context: dict[str, object] | None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_type": "research-triage",
        "subject": entry.get("ticker"),
        "observed_facts": entry.get("candidate_snapshot"),
        "machine_owned_criteria": {
            "allowed_verdicts": ["research", "skip"],
            "unknown_is_not_skip": True,
            "research_requires": ["rationale", "research_question", "key_risk"],
            "skip_requires": ["rationale"],
        },
        "relevant_macro_binding": (
            None
            if macro_context is None
            else {
                "context_id": macro_context_id,
                "content_digest": digest_json(macro_context),
            }
        ),
        "relevant_macro_context_ref": (
            None
            if macro_context is None
            else {
                "path": "shared/macro-context.json",
                "digest": digest_json(macro_context),
            }
        ),
        "rules_digest": rules_digest,
        "policy_version": _POLICY_VERSION,
        "prompt_policy_version": prompt_policy_version,
        "packet_schema_version": 1,
        "required_result_schema": _TRIAGE_SCHEMA,
        "required_result_schema_digest": _TRIAGE_SCHEMA_DIGEST,
        "temporal_validity": _temporal_validity(entry),
        "output_limits": {"rationale": 1200, "research_question": 600, "key_risk": 600},
    }


def _temporal_validity(entry: dict[str, object]) -> object:
    snapshot = entry.get("candidate_snapshot")
    if not isinstance(snapshot, dict):
        return {"quality": "unknown"}
    analysis = snapshot.get("analysis")
    if not isinstance(analysis, dict):
        return {"quality": "unknown"}
    return {
        key: value
        for key, value in analysis.items()
        if "date" in key or "fresh" in key or "stale" in key or "quality" in key or "unknown" in key
    }


def _cache_reuse_allowed(value: object, *, key: str = "") -> bool:
    lowered_key = key.lower()
    if "stale" in lowered_key and value is True:
        return False
    if "unknown" in lowered_key and value not in (False, 0, "", None, [], {}):
        return False
    if "failure" in lowered_key and value not in (False, 0, "", None, [], {}):
        return False
    if isinstance(value, str):
        if value.lower() in {
            "stale",
            "unknown",
            "incomplete",
            "source_incomplete",
        }:
            return False
    elif isinstance(value, dict):
        return all(_cache_reuse_allowed(nested, key=str(name)) for name, nested in value.items())
    elif isinstance(value, list):
        return all(_cache_reuse_allowed(nested, key=key) for nested in value)
    return True


def _cached_result(
    workspace: Workspace, digest: str, *, expected_task_id: str | None = None
) -> AIResult | None:
    path = workspace.state_root / "cache" / "analysis-results" / f"{digest}.json"
    if not path.is_file():
        return None
    try:
        value = read_json(path, root=workspace.state_root)
        result = AIResult.model_validate(value)
    except (OSError, ValueError):
        return None
    if result.input_digest != digest:
        return None
    if expected_task_id is not None and result.task_id != expected_task_id:
        return None
    return result


def prepare_packet(
    workspace: Workspace,
    *,
    root: Path,
    macro_review: bool = False,
    re_evaluate: bool = False,
) -> PacketIndex:
    manifest = workspace.manifest()
    if manifest.get("status") == "skipped_non_business_day":
        index = _empty_packet(workspace, manifest, "no_ai", "daily skipped a non-business day")
        index_digest = _write_index(workspace, index)
        workspace.update(
            state="no_ai",
            status="no_ai",
            current_stage="complete",
            ai_task_count=0,
            reused_task_count=0,
            packet_index_sha256=index_digest,
        )
        return index
    review_set_id = manifest.get("review_set_id")
    if not isinstance(review_set_id, str) or not review_set_id:
        index = _empty_packet(workspace, manifest, "no_ai", "daily produced no Review Set")
        index_digest = _write_index(workspace, index)
        workspace.update(
            state="no_ai",
            status="no_ai",
            current_stage="complete",
            ai_task_count=0,
            reused_task_count=0,
            packet_index_sha256=index_digest,
        )
        return index
    _review_set(root, review_set_id)
    scaffold = _scaffold(root, workspace, review_set_id)
    scaffold_path = workspace.path / "assembled" / "research-triage-scaffold.yaml"
    try:
        operation_id = _bind_operation(root, str(manifest["asof"]))
    except _OperationAsOfConflict as exc:
        if not macro_review:
            raise
        monitor = _macro_monitor(root, asof=str(manifest["asof"]), manual=True)
        workspace.update(
            macro_monitor=monitor,
            triage_deferred_reason="active_operation_binding",
        )
        if monitor["status"] != "review":
            raise
        scaffold_path.unlink(missing_ok=True)
        try:
            macro_task, macro_bytes = _prepare_macro_task(workspace, root=root, manifest=manifest)
        except ValueError as macro_exc:
            return _finalize_packet(
                workspace,
                manifest=manifest,
                tasks=[],
                task_sizes={},
                total_bytes=0,
                machine_failure=str(macro_exc),
            )
        return _finalize_packet(
            workspace,
            manifest=manifest,
            tasks=[macro_task],
            task_sizes={macro_task.task_id: macro_bytes},
            total_bytes=macro_bytes,
            warning=f"research_triage_deferred: {exc}",
        )
    scaffold_digest = hashlib.sha256(scaffold_path.read_bytes()).hexdigest()
    workspace.update(
        macro_context_revision=scaffold.get("macro_context_id"),
        operation_session_id=operation_id,
        research_triage_expected_head=scaffold.get("expected_prior_research_triage_id"),
        triage_scaffold_sha256=scaffold_digest,
    )
    manifest = workspace.manifest()
    macro_context = _prepare_macro_context_projection(
        workspace,
        root=root,
        context_id=scaffold.get("macro_context_id"),
        asof=str(manifest["asof"]),
    )
    entries = scaffold.get("entries")
    if not isinstance(entries, list):
        raise ValueError("research-triage scaffold entries must be an array")
    packet_dir = workspace.path / "packet"
    tasks: list[TaskReference] = []
    task_sizes: dict[str, int] = {}
    total_bytes = 0 if macro_context is None else len(canonical_json(macro_context))
    rules_digest = str(manifest["rules_digest"])
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("ticker"), str):
            raise ValueError("research-triage scaffold entry is invalid")
        payload = _task_payload(
            entry=entry,
            macro_context_id=scaffold.get("macro_context_id"),
            rules_digest=rules_digest,
            prompt_policy_version=str(manifest["prompt_policy_version"]),
            macro_context=macro_context,
        )
        reuse_allowed = _cache_reuse_allowed(payload["observed_facts"])
        payload["cache_reuse_allowed"] = reuse_allowed
        digest = digest_json(payload)
        payload["input_digest"] = digest
        encoded = canonical_json(payload) + b"\n"
        if len(encoded) > _TASK_MAX_BYTES:
            raise ValueError(f"analysis task exceeds {_TASK_MAX_BYTES} bytes: {entry['ticker']}")
        task_id = f"research-triage:{entry['ticker']}"
        relative = f"tasks/{entry['ticker']}.json"
        write_bytes_atomic(packet_dir / relative, encoded, root=workspace.state_root)
        cached = (
            None
            if re_evaluate or not reuse_allowed
            else _cached_result(workspace, digest, expected_task_id=task_id)
        )
        tasks.append(
            TaskReference(
                task_id=task_id,
                type="research-triage",
                subject=entry["ticker"],
                input_digest=digest,
                payload_path=relative,
                required_result_schema=_TRIAGE_SCHEMA,
                reused=cached is not None,
            )
        )
        task_sizes[task_id] = len(encoded)
        total_bytes += len(encoded)
    macro_failure: str | None = None
    monitor = _macro_monitor(root, asof=str(manifest["asof"]), manual=macro_review)
    workspace.update(macro_monitor=monitor)
    if monitor["status"] == "review":
        try:
            macro_task, macro_bytes = _prepare_macro_task(workspace, root=root, manifest=manifest)
        except ValueError as exc:
            macro_failure = str(exc)
        else:
            tasks.append(macro_task)
            task_sizes[macro_task.task_id] = macro_bytes
            total_bytes += macro_bytes
    return _finalize_packet(
        workspace,
        manifest=manifest,
        tasks=tasks,
        task_sizes=task_sizes,
        total_bytes=total_bytes,
        machine_failure=macro_failure,
    )


def _finalize_packet(
    workspace: Workspace,
    *,
    manifest: dict[str, object],
    tasks: list[TaskReference],
    task_sizes: dict[str, int],
    total_bytes: int,
    machine_failure: str | None = None,
    warning: str | None = None,
) -> PacketIndex:
    if total_bytes > _PACKET_MAX_BYTES:
        raise ValueError(f"packet_budget_exceeded: packet exceeds {_PACKET_MAX_BYTES} bytes")
    rules_digest = str(manifest["rules_digest"])
    status: Literal["ai_required", "no_ai", "machine_incomplete"]
    if machine_failure is not None:
        status = "machine_incomplete"
    else:
        status = "ai_required" if any(not task.reused for task in tasks) else "no_ai"
    source_digest = digest_json(_source_binding(manifest))
    index = PacketIndex(
        schema_version=1,
        packet_id=f"packet-{workspace.run_id}",
        run_id=workspace.run_id,
        asof=str(manifest["asof"]),
        source_manifest_digest=source_digest,
        policy_version=_POLICY_VERSION,
        prompt_policy_version=str(manifest["prompt_policy_version"]),
        packet_schema_version=1,
        rules_digest=rules_digest,
        status=status,
        tasks=tuple(tasks),
        batches=_stable_batches(tasks, task_sizes),
        warning=machine_failure or warning,
        required_human_action=(
            "repair macro machine inputs before AI judgment"
            if status == "machine_incomplete"
            else ("provide results for non-reused tasks" if status == "ai_required" else None)
        ),
        packet_bytes=total_bytes,
        estimated_tokens=(total_bytes + 3) // 4,
    )
    index_digest = _write_index(workspace, index)
    workspace.update(
        state=status,
        status=status,
        current_stage=(
            "ai"
            if status == "ai_required"
            else ("machine-input" if status == "machine_incomplete" else "check")
        ),
        ai_task_count=(
            0 if status == "machine_incomplete" else sum(not task.reused for task in tasks)
        ),
        reused_task_count=sum(task.reused for task in tasks),
        packet_index_sha256=index_digest,
    )
    return index


def _stable_batches(
    tasks: list[TaskReference], task_sizes: dict[str, int]
) -> tuple[BatchReference, ...]:
    batches: list[BatchReference] = []
    current_type: Literal["research-triage", "macro-context"] | None = None
    current_ids: list[str] = []
    current_bytes = 0

    def flush() -> None:
        nonlocal current_type, current_ids, current_bytes
        if not current_ids or current_type is None:
            return
        batch_type = current_type
        batches.append(
            BatchReference(
                batch_id=f"{batch_type}:batch-{len(batches) + 1}",
                type=batch_type,
                task_ids=tuple(current_ids),
            )
        )
        current_type = None
        current_ids = []
        current_bytes = 0

    for task in tasks:
        if task.reused:
            continue
        size = task_sizes[task.task_id]
        if current_ids and (task.type != current_type or current_bytes + size > _BATCH_MAX_BYTES):
            flush()
        current_type = task.type
        current_ids.append(task.task_id)
        current_bytes += size
    flush()
    return tuple(batches)


def _prepare_macro_context_projection(
    workspace: Workspace,
    *,
    root: Path,
    context_id: object,
    asof: str,
) -> dict[str, object] | None:
    if context_id is None:
        return None
    if not isinstance(context_id, str) or not context_id:
        raise ValueError("research-triage scaffold has an invalid macro_context_id")
    payload = _run_json(
        [
            "baibai-engine",
            "macro",
            "context",
            "show",
            "--context-id",
            context_id,
            "--asof",
            asof,
            "--format",
            "json",
        ],
        root=root,
    )
    if not isinstance(payload, dict) or payload.get("context_id") != context_id:
        raise ValueError("bound Macro Context JSON does not match the scaffold")
    projection = {
        "schema_version": 1,
        "context_id": context_id,
        "as_of": payload.get("as_of"),
        "summary": payload.get("summary"),
        "synthesis": payload.get("synthesis"),
        "connection": payload.get("connection"),
    }
    encoded = canonical_json(projection)
    if len(encoded) > _SHARED_MAX_BYTES:
        raise ValueError(
            f"packet_budget_exceeded: Macro Context projection exceeds {_SHARED_MAX_BYTES} bytes"
        )
    write_json_atomic(
        workspace.path / "packet" / "shared" / "macro-context.json",
        projection,
        root=workspace.state_root,
    )
    return projection


def _macro_monitor(root: Path, *, asof: str, manual: bool) -> dict[str, object]:
    measured = _run_json(
        [
            "baibai-engine",
            "macro",
            "context",
            "monitor",
            "--asof",
            asof,
            "--format",
            "json",
        ],
        root=root,
    )
    if not isinstance(measured, dict) or measured.get("status") not in {"no_ai", "review"}:
        raise ValueError("macro context monitor returned an invalid machine status")
    if manual:
        return {**measured, "status": "review", "reason": "manual"}
    return measured


def _prepare_macro_task(
    workspace: Workspace, *, root: Path, manifest: dict[str, object]
) -> tuple[TaskReference, int]:
    reading = _run_json(
        ["baibai-engine", "macro", "reading", "--asof", str(manifest["asof"]), "--format", "json"],
        root=root,
    )
    if _contains_incomplete_macro_state(reading):
        raise ValueError("macro reading contains stale, failed, or incomplete required input")
    reading_bytes = len(canonical_json(reading))
    if reading_bytes > _SHARED_MAX_BYTES:
        raise ValueError(f"packet_budget_exceeded: macro reading exceeds {_SHARED_MAX_BYTES} bytes")
    shared_path = workspace.path / "packet" / "shared" / "macro-reading.json"
    write_json_atomic(shared_path, reading, root=workspace.state_root)
    market_snapshot = _run_json(
        [
            "baibai-engine",
            "screening",
            "market-snapshot",
            "--asof",
            str(manifest["asof"]),
            "--format",
            "json",
        ],
        root=root,
    )
    market_bytes = len(canonical_json(market_snapshot))
    if market_bytes > _SHARED_MAX_BYTES:
        raise ValueError(
            f"packet_budget_exceeded: market snapshot exceeds {_SHARED_MAX_BYTES} bytes"
        )
    market_path = workspace.path / "packet" / "shared" / "market-snapshot.json"
    write_json_atomic(market_path, market_snapshot, root=workspace.state_root)
    payload = {
        "schema_version": 1,
        "task_type": "macro-context",
        "phase": "independent_current",
        "asof": manifest["asof"],
        "reading_path": "shared/macro-reading.json",
        "reading_digest": digest_json(reading),
        "market_snapshot_path": "shared/market-snapshot.json",
        "market_snapshot_digest": digest_json(market_snapshot),
        "prior_context_access": "forbidden_in_this_phase",
        "required_counter_evidence": True,
        "policy_version": _POLICY_VERSION,
        "prompt_policy_version": str(manifest["prompt_policy_version"]),
        "packet_schema_version": 1,
        "required_result_schema": _MACRO_SCHEMA,
    }
    digest = digest_json(payload)
    payload["input_digest"] = digest
    encoded = canonical_json(payload) + b"\n"
    relative = "tasks/macro-context.json"
    write_bytes_atomic(workspace.path / "packet" / relative, encoded, root=workspace.state_root)
    return (
        TaskReference(
            task_id="macro-context:independent-current",
            type="macro-context",
            subject=str(manifest["asof"]),
            input_digest=digest,
            payload_path=relative,
            required_result_schema=_MACRO_SCHEMA,
            reused=False,
        ),
        len(encoded) + reading_bytes + market_bytes,
    )


def _contains_incomplete_macro_state(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if (
                str(key).lower() in {"status", "freshness", "quality"}
                and isinstance(nested, str)
                and nested.lower()
                in {
                    "stale",
                    "failed",
                    "failure",
                    "incomplete",
                    "insufficient_history",
                }
            ):
                return True
            if _contains_incomplete_macro_state(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_incomplete_macro_state(item) for item in value)
    return False


def _empty_packet(
    workspace: Workspace,
    manifest: dict[str, object],
    status: Literal["no_ai", "ai_required", "machine_incomplete"],
    warning: str,
) -> PacketIndex:
    return PacketIndex(
        schema_version=1,
        packet_id=f"packet-{workspace.run_id}",
        run_id=workspace.run_id,
        asof=str(manifest["asof"]),
        source_manifest_digest=digest_json(_source_binding(manifest)),
        policy_version=_POLICY_VERSION,
        prompt_policy_version=str(manifest["prompt_policy_version"]),
        packet_schema_version=1,
        rules_digest=str(manifest["rules_digest"]),
        status=status,
        tasks=(),
        batches=(),
        warning=warning,
        packet_bytes=0,
        estimated_tokens=0,
    )


def _write_index(workspace: Workspace, index: PacketIndex) -> str:
    payload = index.model_dump(mode="json")
    encoded = canonical_json(payload) + b"\n"
    if len(encoded) > _INDEX_MAX_BYTES:
        raise ValueError(f"packet index exceeds {_INDEX_MAX_BYTES} bytes")
    write_bytes_atomic(workspace.path / "packet" / "index.json", encoded, root=workspace.state_root)
    return hashlib.sha256(encoded).hexdigest()


def check_results(
    workspace: Workspace, *, results_path: Path, root: Path
) -> tuple[str, Path | None]:
    if workspace.manifest().get("state") not in {"ai_required", "no_ai"}:
        raise ValueError("workspace state does not permit analysis result checking")
    index = validate_packet(workspace)
    if index.status == "machine_incomplete":
        raise ValueError("machine_incomplete packet cannot accept AI results")
    expected_results = workspace.path / "ai" / "results.json"
    if results_path.absolute() != expected_results.absolute():
        raise ValueError("AI results must use the exact workspace ai/results.json path")
    if not results_path.is_file() or results_path.stat().st_size > _RESULT_MAX_BYTES:
        raise ValueError(f"AI results must be a file no larger than {_RESULT_MAX_BYTES} bytes")
    envelope = AIResultEnvelope.model_validate(read_json(results_path, root=workspace.state_root))
    if envelope.packet_id != index.packet_id:
        raise ValueError("AI result packet_id does not match the workspace")
    supplied = {result.task_id: result for result in envelope.results}
    resolved: dict[str, AIResult] = {}
    for task in index.tasks:
        result = (
            _cached_result(workspace, task.input_digest, expected_task_id=task.task_id)
            if task.reused
            else supplied.get(task.task_id)
        )
        if result is None:
            raise ValueError(f"AI result is missing task: {task.task_id}")
        if result.input_digest != task.input_digest:
            raise ValueError(f"AI result input_digest mismatch: {task.task_id}")
        if task.type == "research-triage" and not isinstance(result.judgment, TriageJudgment):
            raise ValueError(f"AI result has the wrong judgment type: {task.task_id}")
        if task.type == "macro-context" and not isinstance(result.judgment, MacroJudgment):
            raise ValueError(f"AI result has the wrong judgment type: {task.task_id}")
        resolved[task.task_id] = result
    unexpected = set(supplied) - {task.task_id for task in index.tasks if not task.reused}
    if unexpected:
        raise ValueError(f"AI result contains unexpected task(s): {sorted(unexpected)}")
    draft = _assemble_triage(workspace, resolved)
    has_macro = any(task.type == "macro-context" for task in index.tasks)
    macro_digest: str | None = None
    if has_macro:
        macro = resolved["macro-context:independent-current"]
        macro_path = workspace.path / "assembled" / "macro-independent.json"
        write_json_atomic(
            macro_path,
            macro.model_dump(mode="json"),
            root=workspace.state_root,
        )
        macro_digest = hashlib.sha256(macro_path.read_bytes()).hexdigest()
    status = "checked"
    # Cache only after every packet/result/scaffold validation and assembly write succeeds.
    for result in resolved.values():
        cache = (
            ensure_private_dir(
                workspace.state_root / "cache" / "analysis-results", root=workspace.state_root
            )
            / f"{result.input_digest}.json"
        )
        write_json_atomic(cache, result.model_dump(mode="json"), root=workspace.state_root)
    workspace.update(
        state=status,
        status=status,
        current_stage="publish",
        model_invocation_count=len(index.batches),
        macro_phase="independent_complete" if has_macro else None,
        macro_independent_sha256=macro_digest,
    )
    return status, draft


def validate_packet(workspace: Workspace) -> PacketIndex:
    """Recheck the exact packet and all machine-owned inputs without writing state."""

    index_raw = read_json(workspace.path / "packet" / "index.json", root=workspace.state_root)
    index = PacketIndex.model_validate(index_raw)
    encoded_index = canonical_json(index.model_dump(mode="json")) + b"\n"
    if hashlib.sha256(encoded_index).hexdigest() != workspace.manifest().get("packet_index_sha256"):
        raise ValueError("packet index digest differs from the workspace manifest")
    if index.source_manifest_digest != digest_json(_source_binding(workspace.manifest())):
        raise ValueError("workspace source manifest binding changed after packet generation")
    for task in index.tasks:
        task_raw = read_json(
            workspace.path / "packet" / task.payload_path, root=workspace.state_root
        )
        if not isinstance(task_raw, dict):
            raise ValueError(f"task payload is not an object: {task.task_id}")
        if task.type == "macro-context":
            for name in ("reading", "market_snapshot"):
                relative = task_raw.get(f"{name}_path")
                expected = task_raw.get(f"{name}_digest")
                if not isinstance(relative, str) or not relative.startswith("shared/"):
                    raise ValueError(f"macro task has an invalid {name} reference")
                shared = read_json(workspace.path / "packet" / relative, root=workspace.state_root)
                if digest_json(shared) != expected:
                    raise ValueError(f"macro task {name} digest mismatch")
        elif task.type == "research-triage":
            macro_ref = task_raw.get("relevant_macro_context_ref")
            if macro_ref is not None:
                if not isinstance(macro_ref, dict):
                    raise ValueError("research-triage task has an invalid macro context ref")
                relative = macro_ref.get("path")
                expected = macro_ref.get("digest")
                if relative != "shared/macro-context.json" or not isinstance(expected, str):
                    raise ValueError("research-triage task has an invalid macro context ref")
                shared = read_json(workspace.path / "packet" / relative, root=workspace.state_root)
                if digest_json(shared) != expected:
                    raise ValueError("research-triage macro context digest mismatch")
        recorded_digest = task_raw.pop("input_digest", None)
        if recorded_digest != task.input_digest or digest_json(task_raw) != task.input_digest:
            raise ValueError(f"task payload digest mismatch: {task.task_id}")
        if (
            task.reused
            and _cached_result(workspace, task.input_digest, expected_task_id=task.task_id) is None
        ):
            raise ValueError(f"reused task cache is missing or invalid: {task.task_id}")
    scaffold = workspace.path / "assembled" / "research-triage-scaffold.yaml"
    expected_scaffold = workspace.manifest().get("triage_scaffold_sha256")
    if (
        scaffold.is_file()
        and hashlib.sha256(scaffold.read_bytes()).hexdigest() != expected_scaffold
    ):
        raise ValueError("machine-owned research-triage scaffold digest changed")
    return index


def _assemble_triage(workspace: Workspace, results: dict[str, AIResult]) -> Path | None:
    path = workspace.path / "assembled" / "research-triage-scaffold.yaml"
    if not path.is_file():
        return None
    expected_digest = workspace.manifest().get("triage_scaffold_sha256")
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_digest:
        raise ValueError("machine-owned research-triage scaffold digest changed")
    draft = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(draft, dict) or not isinstance(draft.get("entries"), list):
        raise ValueError("research-triage scaffold is invalid")
    research_priority = 0
    for entry in draft["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("ticker"), str):
            raise ValueError("research-triage scaffold entry is invalid")
        result = results.get(f"research-triage:{entry['ticker']}")
        if result is None or not isinstance(result.judgment, TriageJudgment):
            raise ValueError(f"missing Research Triage result: {entry['ticker']}")
        judgment = result.judgment
        entry["decision"] = judgment.verdict
        entry["rationale"] = judgment.rationale
        entry["research_question"] = judgment.research_question
        entry["key_risk"] = judgment.key_risk
        if judgment.verdict == "research":
            research_priority += 1
            entry["priority"] = research_priority
        else:
            entry["priority"] = None
    draft["research_triage_id"] = (
        f"research-triage-{str(draft['as_of']).replace('-', '')}-{workspace.run_id[:12]}"
    )
    publication_time = workspace.manifest().get("publication_time")
    if not isinstance(publication_time, str):
        raise ValueError("workspace has no fixed publication_time")
    draft["published_at"] = datetime.fromisoformat(publication_time).astimezone(_JST).isoformat()
    output = workspace.path / "assembled" / "research-triage.yaml"
    rendered = yaml.safe_dump(draft, sort_keys=False, allow_unicode=True).encode()
    write_bytes_atomic(output, rendered, root=workspace.state_root)
    return output


__all__ = ["check_results", "prepare_packet", "validate_packet"]
