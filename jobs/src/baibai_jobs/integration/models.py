"""固定入力からモデルと候補を生成し、同じlake releaseを公開する。Web生成は行わない。"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

from baibai_engine.data_collection.providers.jquants.financial_native import (
    FinancialL1,
    FinancialObjectStore,
)
from baibai_engine.data_collection.receipts.native_sources.store import (
    DEFAULT_DB_PATH as DEFAULT_ECONOMIC_DB_PATH,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot as ModelInputSnapshot
from baibai_engine.machine_analysis.generation.inputs import read_and_generate_models
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import (
    InputRelease as ModelInputRelease,
)
from baibai_engine.machine_analysis.screening.policies.economic_path_selection import (
    economic_selection_id,
    evaluate_saved_economic_generation,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import EconomicPathPolicy
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
)
from baibai_engine.storage.paths import MARKET_DB_PATH, RUNS_DB_PATH
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.sqlite.snapshot import create_snapshot
from baibai_jobs.financial_cutover import financial_native_enabled
from baibai_jobs.integration.financial_native import model_input as native_financial_model_input
from baibai_jobs.maintenance.model_cache import prune_model_cache
from baibai_jobs.publication.lease import _load_handle, verify_held
from baibai_jobs.publication.snapshot_publication import (
    prepare_market_lake,
    publish_prepared_market_lake,
)


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
) -> dict[str, object]:
    """leaseで順序を守り、モデル生成後に別releaseを作らない。"""
    handle = _load_handle(lease_handle)
    verify_held(store, handle)
    mirror.mkdir(parents=True, exist_ok=True)
    evaluated_at = datetime.now(UTC)
    financial_input = None
    if financial_native_enabled():
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
    with (
        prepare_market_lake(
            sqlite_path=market, mirror_root=mirror, store=store, as_of=as_of
        ) as prepared,
        tempfile.TemporaryDirectory(prefix=".model-economic-", dir=mirror) as directory,
    ):
        economic_copy = Path(directory) / "economic.sqlite"
        create_snapshot(economic, economic_copy)
        digest = sha256_file(economic_copy)
        generation = read_and_generate_models(
            generation_id=f"models-{prepared.release.release_id}",
            market_snapshot=prepared.source_snapshot,
            market_release=ModelInputRelease(
                release_id=prepared.release.release_id,
                manifest_sha256=prepared.release_sha256,
            ),
            economic_path=economic_copy,
            economic_identity=ModelInputSnapshot(
                kind="sqlite_snapshot", reference=f"economic-{digest[:24]}", sha256=digest
            ),
            as_of=as_of,
            evaluation_time=max(evaluated_at, prepared.source_snapshot.sealed_at),
            ttm_rules=DEFAULT_TTM_RULES,
            growth_start=growth_start,
            financial_input=financial_input,
            price_identity=prepared.price_identity,
        )
        generations = ModelGenerationStore(runs)
        saved = generations.publish_stream(generation)
        subjects = generation.subjects
        evaluation = evaluate_saved_economic_generation(
            generation.metadata.generation_id, tickers=subjects, policy=policy, path=runs
        )
        selection = evaluation.selection
        selection_id = economic_selection_id(generation.metadata.generation_id, policy=policy)
        ScreeningSelectionStore(runs).publish(selection_id, selection)
        publication = publish_prepared_market_lake(
            prepared, store=store, before_pointer_switch=lambda: verify_held(store, handle)
        )
    pruned = prune_model_cache(runs, keep=3, preserve=(generation.metadata.generation_id,))
    return {
        "pruned_generation_count": len(pruned),
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
        "economic_snapshot_sha256": digest,
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
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
