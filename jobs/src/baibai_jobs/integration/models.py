"""固定入力からモデルと候補を生成し、同じlake releaseを公開する。Web生成は行わない。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

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
from baibai_engine.machine_analysis.generation.inputs import read_and_generate_models
from baibai_engine.machine_analysis.generation.query import (
    read_generation_ids,
    read_generation_storage_totals,
)
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import (
    InputRelease as ModelInputRelease,
)
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import project_company
from baibai_engine.machine_analysis.screening.policies.economic_path_selection import (
    economic_selection_id,
    evaluate_economic_coordinates,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CompanyCoordinates,
    EconomicPathPolicy,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
)
from baibai_engine.storage.paths import MARKET_DB_PATH, RUNS_DB_PATH
from baibai_jobs.integration.financial_native import model_input as native_financial_model_input
from baibai_jobs.maintenance.model_cache import prune_model_cache
from baibai_jobs.publication.lease import _load_handle, verify_held
from baibai_jobs.publication.snapshot_publication import (
    prepare_market_lake,
    publish_prepared_market_lake,
)


def _phase_progress(phase: str, started: float | None = None) -> float:
    now = time.perf_counter()
    state = "start" if started is None else f"done elapsed_seconds={now - started:.3f}"
    print(f"model integration: phase={phase} state={state}", file=sys.stderr, flush=True)
    return now


def _project_company_models(
    generation: ModelGenerationStream, coordinates: dict[str, CompanyCoordinates]
) -> Iterator[GeneratedModel]:
    # 通常assemblyは個社の全モデルを続けて返し、その後に共有subjectを返す。
    # 順序が変わった列を部分座標で選ばず、writerのtransaction内で止める。
    subjects = set(generation.subjects)
    models: list[GeneratedModel] = []

    def project() -> None:
        if models:
            subject = models[0].subject
            coordinates[subject] = project_company(
                subject, models=models, metadata=generation.metadata
            )
            models.clear()

    for model in generation.models:
        if models and models[0].subject != model.subject:
            project()
        if model.subject in subjects:
            if not models and model.subject in coordinates:
                raise ValueError("model integration requires contiguous company models")
            models.append(model)
        yield model
    project()


def integrate_models(
    *,
    store: FinancialObjectStore,
    market: Path,
    economic: Path,
    runs: Path,
    mirror: Path,
    as_of: date,
    lease_handle: Path,
    growth_start: date | None,
    policy: EconomicPathPolicy,
    edinet_input: InputSnapshot | None = None,
    preserve_generation_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    """leaseで順序を守り、モデル生成後に別releaseを作らない。"""
    handle = _load_handle(lease_handle)
    verify_held(store, handle)
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
    # This is the downloaded execution copy, not L1 or application data.
    # It must not retain an unpublished old projection when push-machine
    # later verifies/dehydrates the new release.
    with sqlite3.connect(market) as connection:
        connection.execute("DELETE FROM jquants_fin_summaries")
        connection.execute("DELETE FROM source_coverage WHERE source='jquants_fin_summaries'")
    _phase_progress("native_financial", phase_started)
    with prepare_market_lake(
        sqlite_path=market,
        mirror_root=mirror,
        store=store,
        as_of=as_of,
        edinet_input=edinet_input,
    ) as prepared:
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
            calendar_identity=prepared.calendar_identity,
            margin_alerts_identity=prepared.margin_alerts_identity,
            margin_interest_identity=prepared.margin_interest_identity,
            short_sale_reports_identity=prepared.short_sale_reports_identity,
        )
        _phase_progress("model_inputs", phase_started)
        phase_started = _phase_progress("model_generation")
        generations = ModelGenerationStore(runs)
        coordinates: dict[str, CompanyCoordinates] = {}
        saved = generations.publish_stream(
            replace(generation, models=_project_company_models(generation, coordinates))
        )
        try:
            storage_totals = read_generation_storage_totals(
                generation.metadata.generation_id, path=runs
            )
        except (sqlite3.DatabaseError, OSError) as error:
            logging.getLogger(__name__).warning(
                "model storage: state=unmeasured error_type=%s", type(error).__name__
            )
        else:
            for model_id, count, size in storage_totals:
                print(
                    f"model storage: model_id={model_id} count={count} payload_gzip_bytes={size}",
                    file=sys.stderr,
                    flush=True,
                )
        _phase_progress("model_generation", phase_started)
        subjects = generation.subjects
        phase_started = _phase_progress("screening")
        evaluation = evaluate_economic_coordinates(
            generation.metadata, tickers=subjects, coordinates=coordinates, policy=policy
        )
        selection = evaluation.selection
        selection_id = economic_selection_id(generation.metadata.generation_id, policy=policy)
        ScreeningSelectionStore(runs).publish(selection_id, selection)
        _phase_progress("screening", phase_started)
        phase_started = _phase_progress("lake_publish")
        publication = publish_prepared_market_lake(
            prepared, store=store, before_pointer_switch=lambda: verify_held(store, handle)
        )
        _phase_progress("lake_publish", phase_started)
    phase_started = _phase_progress("model_cache_prune")
    try:
        pruned = prune_model_cache(
            runs,
            keep=3,
            preserve=(*preserve_generation_ids, generation.metadata.generation_id),
        )
    except (OSError, sqlite3.Error) as error:
        # Cache retention is optional; store upload still checks the SQLite snapshot.
        logging.getLogger(__name__).warning(
            "model cache prune: state=failed error_type=%s", type(error).__name__
        )
        pruned_count = None
    else:
        pruned_count = len(pruned)
    _phase_progress("model_cache_prune", phase_started)
    return {
        "pruned_generation_count": pruned_count,
        "asof": as_of.isoformat(),
        "models_ready": True,
        "generation_id": generation.metadata.generation_id,
        "selection_id": selection_id,
        "selection_method": policy.method,
        "company_count": len(subjects),
        "model_count": saved.model_count,
        "candidate_count": len(selection.result.tickers),
        "screening_price_inputs": evaluation.price_inputs,
        "growth_start": None if growth_start is None else growth_start.isoformat(),
        "economic_input_sha256": economic_input.identity.sha256,
        "lake": publication.as_dict(),
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
        "--growth-start",
        type=date.fromisoformat,
        help="optional accounting-period start; omitted retains all available periods",
    )
    parser.add_argument("--candidate-limit", type=int, default=36)
    parser.add_argument("--integration-report", type=Path)
    parser.add_argument(
        "--preserve-existing-generations",
        action="store_true",
        help="retain every existing model generation and its selections for this integration",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        edinet_input = None
        if args.integration_report is not None:
            report = json.loads(args.integration_report.read_bytes())
            if report["asof"] != args.asof.isoformat():
                raise ValueError("source integration report differs from the model date")
            if report.get("edinet_input") is not None:
                edinet_input = InputSnapshot.model_validate(report["edinet_input"])
        preserve_generation_ids = (
            read_generation_ids(path=args.runs) if args.preserve_existing_generations else ()
        )
        result = integrate_models(
            store=Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores"),
            market=args.market,
            economic=args.economic,
            runs=args.runs,
            mirror=args.mirror,
            as_of=args.asof,
            lease_handle=args.lease_handle,
            growth_start=args.growth_start,
            policy=EconomicPathPolicy(candidate_limit=args.candidate_limit),
            edinet_input=edinet_input,
            preserve_generation_ids=preserve_generation_ids,
        )
    except Exception as error:
        write_json_atomic(
            args.notice_output,
            {
                "asof": args.asof.isoformat(),
                "models_ready": False,
                "failed_stage": "model-integration",
                "error_type": type(error).__name__,
            },
        )
        print(f"model integration failed: {type(error).__name__}", file=sys.stderr)
        return 1
    write_json_atomic(args.notice_output, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
