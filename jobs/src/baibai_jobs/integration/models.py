"""固定入力からモデルと候補を生成し、同じlake releaseを公開する。Web生成は行わない。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, nullcontext, suppress
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol, TypedDict

from baibai_engine.data_collection.providers.jquants.financial_native import (
    FinancialL1,
    FinancialObjectStore,
)
from baibai_engine.data_collection.receipts.native_sources.store import (
    DEFAULT_DB_PATH as DEFAULT_ECONOMIC_DB_PATH,
)
from baibai_engine.machine_analysis.generation.comparison_evidence import SavedComparisonEvidence
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    InputSnapshot,
    ModelGenerationStream,
)
from baibai_engine.machine_analysis.generation.economic_inputs import (
    EconomicInputSession,
)
from baibai_engine.machine_analysis.generation.full_day_inputs import share_full_day_input_reads
from baibai_engine.machine_analysis.generation.inputs import (
    PinnedGenerationMaster,
    read_and_generate_models,
)
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import (
    InputRelease as ModelInputRelease,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    read_local_listing_master,
)
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DiscoveryRow,
    DiscoverySelection,
    discovery_publication_id,
)
from baibai_engine.machine_analysis.screening.policies.discovery import (
    evaluate_discovery_rows,
    project_discovery_row,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    LakePublishError,
    R2OperationStopped,
)
from baibai_engine.storage.paths import MARKET_DB_PATH, RUNS_DB_PATH
from baibai_engine.storage.snapshots.keys import release_manifest_key
from baibai_engine.storage.snapshots.models import ReleasePolicyError
from baibai_engine.storage.snapshots.objects import LakeObjectError, LocalMirrorSource, mirror_path
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_engine.storage.snapshots.release import LakeReleasePointer
from baibai_engine.storage.snapshots.retention import exclusive_lock
from baibai_engine.storage.sqlite.snapshot import create_snapshot
from baibai_jobs.integration.financial_native import model_input as native_financial_model_input
from baibai_jobs.integration.listing_master_current import LISTING_MASTER_CURRENT_KEY
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.lease import _load_handle, verify_held
from baibai_jobs.publication.machine_current import (
    MAX_MACHINE_PUT_BYTES,
    MachineWriteStore,
    ReadyMachineBase,
    finish_machine_retirement,
    publish_staged_machine,
    read_machine_current,
    require_market_origin,
    require_runs_publication,
)
from baibai_jobs.publication.machine_origin import current_daily_run
from baibai_jobs.publication.policy import (
    edinet_metrics_contract_is_current,
    release_policy_for_profile,
)
from baibai_jobs.publication.snapshot_publication import (
    PreparedMarketLake,
    daily_external_readback_dir,
    prepare_market_lake,
    stage_prepared_market_lake,
)
from baibai_jobs.publication.snapshot_release import (
    PointerSnapshot,
    StagedLake,
    read_pointer_snapshot,
)
from baibai_jobs.publication.snapshot_write_cli import prepare_market_transfer

_LEGACY_MASTER_DATASET = "jquants.master_snapshots"


class ModelIntegrationStore(FinancialObjectStore, MachineWriteStore, Protocol):
    """Lake/native preparation and the exact Machine operations needed by this job."""


def pin_current_jp_generation_master(path: Path) -> PinnedGenerationMaster:
    """Pin the JP section from one local artifact without rereading the remote head."""
    section = read_local_listing_master(path).get("JP")
    if section is None:
        raise ValueError("current JP master is unavailable")
    return PinnedGenerationMaster(section, f"{LISTING_MASTER_CURRENT_KEY}#JP")


def _phase_progress(phase: str, started: float | None = None) -> float:
    now = time.perf_counter()
    state = "start" if started is None else f"done elapsed_seconds={now - started:.3f}"
    print(f"model integration: phase={phase} state={state}", file=sys.stderr, flush=True)
    return now


def _project_company_models(
    generation: ModelGenerationStream, rows: dict[str, DiscoveryRow]
) -> Iterator[GeneratedModel]:
    # 通常assemblyは個社の全モデルを続けて返し、その後に共有subjectを返す。
    # 順序が変わった列を部分判定で選ばず、writerのtransaction内で止める。
    subjects = set(generation.subjects)
    models: list[GeneratedModel] = []

    def project() -> None:
        if models:
            subject = models[0].subject
            rows[subject] = project_discovery_row(
                subject, models=tuple(models), metadata=generation.metadata
            )
            models.clear()

    for model in generation.models:
        if models and models[0].subject != model.subject:
            project()
        if model.subject in subjects:
            if not models and model.subject in rows:
                raise ValueError("model integration requires contiguous company models")
            models.append(model)
        yield model
    project()


class _ReadbackOptions(TypedDict, total=False):
    retain_verified_readback: Callable[[str, Path], None]


def _external_readback_options(
    prepared: PreparedMarketLake,
    directory: Path | None,
    partitions: frozenset[tuple[str, int, int]],
) -> _ReadbackOptions:
    if directory is None or not partitions:
        return {}
    # Retain only a bounded subset needed by this attempt's semantic verification.
    root = directory / prepared.release_sha256
    try:
        remaining = min(256 * 1024**2, shutil.disk_usage(directory.parent.parent).free // 16)
        root.mkdir(mode=0o700, parents=True)
    except OSError:  # Includes a second invocation in the same attempt.
        return {}
    keys: set[str] = set()
    for name, year, month in sorted(partitions):
        if (dataset := prepared.export.datasets.get(name)) is None:
            continue
        for partition in dataset.manifest.partitions:
            if partition.values != {"year": year, "month": month}:
                continue
            for obj in partition.objects:
                if obj.key not in keys and obj.bytes <= remaining:
                    keys.add(obj.key)
                    remaining -= obj.bytes
    if not keys:
        return {}

    def retain(key: str, sealed: Path) -> None:
        if key in keys:
            with suppress(LakeObjectError):
                destination = mirror_path(root, key)
                destination.parent.mkdir(parents=True, exist_ok=True)
                sealed.replace(destination)

    return {"retain_verified_readback": retain}


def _publish_machine(
    *,
    store: MachineWriteStore,
    staged: StagedLake,
    base: ReadyMachineBase,
    market: Path,
    runs: Path,
    mirror: Path,
    bucket: str | None,
    while_held: Callable[[], None],
) -> dict[str, object]:
    # The preparation context (large SQLite seal) has closed before these two copies.
    release = resolve_release(
        LocalMirrorSource(mirror),
        staged.pointer.release_id,
        manifest_sha256=staged.pointer.manifest_sha256,
    )
    if edinet_metrics_contract_is_current(release.manifest) is False:
        raise LakePublishError("cannot publish obsolete EDINET metrics as current")
    with tempfile.TemporaryDirectory(prefix=".machine-publish-", dir=mirror) as directory:
        transfer_market = Path(directory) / "market.sqlite"
        transfer_runs = Path(directory) / "runs.sqlite"
        prepare_market_transfer(
            market,
            transfer_market,
            release=release,
            mirror=mirror,
            bucket=bucket,
        )
        create_snapshot(runs, transfer_runs, compact=True)
        while_held()
        committed, retired = publish_staged_machine(
            store,
            lake=staged,
            base=base,
            market=transfer_market,
            runs=transfer_runs,
            while_held=while_held,
        )
    pointer = committed.current.pointer
    assert pointer is not None
    assert pointer.machine is not None
    return {
        "market_bytes": pointer.machine.market.size,
        "runs_bytes": pointer.machine.runs.size,
        "single_put_limit_bytes": MAX_MACHINE_PUT_BYTES,
        "superseded_members_removed": retired,
    }


def _require_closed_runs_target(runs: Path) -> None:
    if runs.is_symlink() or (runs.exists() and not runs.is_file()):
        raise ValueError("Runs working destination is not a regular file")
    for suffix in ("-wal", "-journal", "-shm"):
        sidecar = runs.with_name(runs.name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise ValueError("close local SQLite users before replacing the Runs working file")


def integrate_models(
    *,
    store: ModelIntegrationStore,
    market: Path,
    economic: Path,
    runs: Path,
    mirror: Path,
    as_of: date,
    lease_handle: Path,
    growth_start: date | None,
    edinet_input: InputSnapshot | None = None,
    current_master: PinnedGenerationMaster | None = None,
    base: PointerSnapshot | None = None,
    machine_store: MachineWriteStore | None = None,
    bucket: str | None = None,
    external_readback_dir: Path | None = None,
    external_partitions: frozenset[tuple[str, int, int]] = frozenset(),
) -> dict[str, object]:
    """Publish under the lease; take a closed, dedicated local working copy."""
    if current_master is not None and any(
        item.dataset == _LEGACY_MASTER_DATASET
        for item in release_policy_for_profile("production").datasets
    ):
        raise ValueError("current listing master requires the no-master Market release policy")
    handle = _load_handle(lease_handle)
    machine_writer = store if machine_store is None else machine_store

    def while_held() -> None:
        verify_held(machine_writer, handle)

    while_held()
    _require_closed_runs_target(runs)
    snapshot = read_pointer_snapshot(machine_writer) if base is None else base
    if base is not None:
        if base.pointer is None or base.pointer.machine is None:
            raise LakePublishError("current machine pair is required before model integration")
        require_market_origin(base.pointer, market)
        require_runs_publication(base.pointer, runs)
    # Complete the former pair before publishing a new Lake graph or Machine.
    ready = finish_machine_retirement(machine_writer, snapshot, while_held=while_held)

    mirror.mkdir(parents=True, exist_ok=True)
    if edinet_input is None:
        raise ValueError("normal model integration requires its fixed EDINET input")
    phase_started = _phase_progress("native_financial")
    # One head read fixes both source membership and confirmation/failure state.
    fixed = FinancialL1(store, mirror / "financial-native").fixed()
    evaluated_at = datetime.now(UTC)
    financial_input = native_financial_model_input(
        store, fixed, as_of=as_of, evaluated_at=evaluated_at
    )
    # The execution copy must not carry the old projection into the new transfer.
    with sqlite3.connect(market) as connection:
        connection.execute("DELETE FROM jquants_fin_summaries")
        connection.execute("DELETE FROM source_coverage WHERE source='jquants_fin_summaries'")
    _phase_progress("native_financial", phase_started)
    runs.parent.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix=".runs-next-", dir=runs.parent) as directory,
        ExitStack() as run,
    ):
        next_runs = Path(directory) / "runs.sqlite"
        # Only the native preparation stages share immutable reads; drop those bytes
        # before models, publication and any subsequent independent read.
        with share_full_day_input_reads(store):
            prepared = run.enter_context(
                prepare_market_lake(
                    sqlite_path=market,
                    mirror_root=mirror,
                    store=store,
                    as_of=as_of,
                    edinet_input=edinet_input,
                    base=snapshot,
                )
            )
        if current_master is not None and (
            prepared.master_identity is not None
            or _LEGACY_MASTER_DATASET in prepared.release.datasets
        ):
            raise ValueError("current listing master cannot accompany a legacy Market master")
        evaluation_time = max(evaluated_at, prepared.source_snapshot.sealed_at)
        phase_started = _phase_progress("economic_input")
        economic_requests = (
            store.serial_requests(min_interval_seconds=0.25)
            if isinstance(store, Boto3R2Store)
            else nullcontext()
        )
        with economic_requests:
            economic_session = EconomicInputSession(store)
            step_started = _phase_progress("economic_input_retain")
            saved_economic = economic_session.retain(
                economic, as_of=as_of, evaluation_time=evaluation_time
            )
            _phase_progress("economic_input_retain", step_started)
            step_started = _phase_progress("economic_input_rebuild")
            economic_input = economic_session.rebuild(economic, saved_economic)
            _phase_progress("economic_input_rebuild", step_started)
        _phase_progress("economic_input", phase_started)
        phase_started = _phase_progress("model_inputs")
        generation = read_and_generate_models(
            generation_id=f"models-{prepared.release.release_id}",
            market_snapshot=prepared.source_snapshot,
            market_release=ModelInputRelease(
                release_id=prepared.release.release_id,
                manifest_sha256=prepared.release_sha256,
            ),
            economic_input=economic_input,
            as_of=as_of,
            evaluation_time=evaluation_time,
            ttm_rules=DEFAULT_TTM_RULES,
            growth_start=growth_start,
            financial_input=financial_input,
            edinet_input=prepared.edinet_input,
            comparison_evidence=SavedComparisonEvidence(
                market=InputSnapshot(
                    kind="lake_release",
                    reference=prepared.serving.pointer.release_id,
                    sha256=prepared.serving.pointer.manifest_sha256,
                ),
                runs=runs,
            )
            if prepared.serving.pointer is not None
            else None,
            price_identity=prepared.price_identity,
            master_identity=prepared.master_identity,
            current_master=current_master,
            calendar_identity=prepared.calendar_identity,
            margin_alerts_identity=prepared.margin_alerts_identity,
            margin_interest_identity=prepared.margin_interest_identity,
            short_sale_reports_identity=prepared.short_sale_reports_identity,
        )
        _phase_progress("model_inputs", phase_started)
        phase_started = _phase_progress("model_generation")
        generations = ModelGenerationStore(next_runs)
        rows: dict[str, DiscoveryRow] = {}
        saved = generations.publish_stream(
            replace(generation, models=_project_company_models(generation, rows))
        )
        _phase_progress("model_generation", phase_started)
        subjects = generation.subjects
        phase_started = _phase_progress("screening")
        selection = evaluate_discovery_rows(generation.metadata, tickers=subjects, rows=rows)
        assert isinstance(selection.result, DiscoverySelection)
        selection_id = discovery_publication_id(generation.metadata.generation_id)
        ScreeningSelectionStore(next_runs).publish(selection_id, selection)
        require_runs_publication(
            LakeReleasePointer(
                release_id=prepared.release.release_id,
                manifest_key=release_manifest_key(release_id=prepared.release.release_id),
                manifest_sha256=prepared.release_sha256,
            ),
            next_runs,
        )
        _phase_progress("screening", phase_started)
        with exclusive_lock(market.parent / ".machine-pull.lock", subject="machine pull"):
            _require_closed_runs_target(runs)
            phase_started = _phase_progress("lake_stage")
            readback_options = _external_readback_options(
                prepared, external_readback_dir, external_partitions
            )
            staged, publication = stage_prepared_market_lake(
                prepared, store=store, **readback_options
            )
            _phase_progress("lake_stage", phase_started)
            # Free the large input seal before making the transfer copies. The
            # working Runs stays the previous seed until the next pair is current.
            run.close()
            phase_started = _phase_progress("machine_publish")
            committed = _publish_machine(
                store=machine_writer,
                staged=staged,
                base=ready,
                market=market,
                runs=next_runs,
                mirror=mirror,
                bucket=bucket,
                while_held=while_held,
            )
            # The same-run JP input probe uses this path rather than another R2 pull.
            try:
                _require_closed_runs_target(runs)
                next_runs.replace(runs)
            except (OSError, ValueError):
                # The pointer is already confirmed. A failed local rename leaves the
                # predecessor intact and is not a failed remote publication.
                committed["local_runs_refreshed"] = False
                print(
                    "machine published; local Runs refresh failed; pull-machine to refresh it",
                    file=sys.stderr,
                )
            else:
                committed["local_runs_refreshed"] = True
            _phase_progress("machine_publish", phase_started)
    return {
        "machine": committed,
        "asof": as_of.isoformat(),
        "models_ready": True,
        "generation_id": generation.metadata.generation_id,
        "selection_id": selection_id,
        "selection_method": selection.result.policy.method,
        "company_count": len(subjects),
        "model_count": saved.model_count,
        "candidate_count": len(selection.result.tickers),
        "screening_price_inputs": selection.price_inputs,
        "screening_summary": selection.result.summary,
        "growth_start": None if growth_start is None else growth_start.isoformat(),
        "economic_input_sha256": economic_input.identity.sha256,
        "lake": publication.as_dict(),
        **({"daily_external_readback": True} if readback_options else {}),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", type=date.fromisoformat, required=True)
    parser.add_argument("--market", type=Path, default=MARKET_DB_PATH)
    parser.add_argument("--economic", type=Path, default=DEFAULT_ECONOMIC_DB_PATH)
    parser.add_argument("--runs", type=Path, default=RUNS_DB_PATH)
    parser.add_argument("--mirror", type=Path, default=Path("stores"))
    parser.add_argument("--lease-handle", type=Path, required=True)
    parser.add_argument("--notice-output", type=Path, required=True)
    parser.add_argument(
        "--daily-origin",
        action="store_true",
        help="bind a successful notice to the current Actions run/attempt",
    )
    parser.add_argument(
        "--growth-start",
        type=date.fromisoformat,
        help="optional accounting-period start; omitted retains all available periods",
    )
    parser.add_argument("--integration-report", type=Path)
    parser.add_argument(
        "--current-listing-master",
        type=Path,
        help="same-run pinned listing master; requires the no-master Market release",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        current_master = (
            pin_current_jp_generation_master(args.current_listing_master)
            if args.current_listing_master is not None
            else None
        )
        publication_run = current_daily_run() if args.daily_origin else None
        bucket = os.environ.get("R2_STORES_BUCKET") or "baibai-stores"
        external_readback_dir = None
        external_partitions: frozenset[tuple[str, int, int]] = frozenset()
        edinet_input = None
        if args.integration_report is not None:
            report = json.loads(args.integration_report.read_bytes())
            if report["asof"] != args.asof.isoformat():
                raise ValueError("source integration report differs from the model date")
            if report.get("edinet_input") is not None:
                edinet_input = InputSnapshot.model_validate(report["edinet_input"])
            if publication_run is not None and (external := report.get("external")):
                external_partitions = frozenset(
                    (str(name), int(year), int(month)) for name, year, month in external["changed"]
                )
                external_readback_dir = daily_external_readback_dir(
                    args.integration_report.parent, bucket=bucket, run=publication_run
                )
        with (
            closing(Boto3R2Store(bucket=bucket)) as store,
            closing(
                Boto3R2Store(
                    bucket=bucket,
                    env={**os.environ, "R2_STOP_ON_REJECTION": "1"},
                    verify_put_with_head=False,
                )
            ) as machine_store,
        ):
            result = integrate_models(
                store=store,
                machine_store=machine_store,
                bucket=bucket,
                base=read_machine_current(machine_store),
                market=args.market,
                economic=args.economic,
                runs=args.runs,
                mirror=args.mirror,
                as_of=args.asof,
                lease_handle=args.lease_handle,
                growth_start=args.growth_start,
                edinet_input=edinet_input,
                current_master=current_master,
                external_readback_dir=external_readback_dir,
                external_partitions=external_partitions,
            )
    except (Exception, R2OperationStopped) as error:
        notice: dict[str, object] = {
            "asof": args.asof.isoformat(),
            "models_ready": False,
            "failed_stage": "model-integration",
            "error_type": type(error).__name__,
        }
        if isinstance(error, ReleasePolicyError):
            diagnostic = {"reason": error.reason.value}
            if error.dataset is not None and error.dataset in LAKE_DATASETS:
                diagnostic["dataset"] = error.dataset
            notice["release_acceptance"] = diagnostic
            # Never emit exception text, dates, paths, source identities or payloads.
            print("model integration acceptance: " + json.dumps(diagnostic), file=sys.stderr)
        write_json_atomic(args.notice_output, notice)
        print(f"model integration failed: {type(error).__name__}", file=sys.stderr)
        return 1
    if publication_run is not None:
        result["daily_run"] = publication_run
    write_json_atomic(args.notice_output, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
