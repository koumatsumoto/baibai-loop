"""保存世代への候補政策だけの発行。モデル生成・lake公開は呼ばない。"""

import json
import sqlite3
from datetime import UTC, date, datetime

from pydantic import TypeAdapter
from tests.model_generation_fixtures import generation

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationSubject,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.models.company_financials.earnings import EarningsPrice
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    EconomicPathPolicy,
    EconomicPathSelection,
)
from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_jobs.cli import main as jobs_main
from baibai_jobs.integration.screening_selection import main


def saved_generation(path):
    fixed = ModelGeneration(
        schema_version=2,
        generation_id="models-fixed-release",
        as_of=date(2026, 10, 23),
        evaluation_time=datetime(2026, 10, 25, tzinfo=UTC),
        inputs={
            "market": InputSnapshot(kind="lake_release", reference="fixed-release", sha256="a" * 64)
        },
        subject_plan={
            "1234": GenerationSubject(
                scope="stock",
                model_ids=("company_financials", "market_context"),
                related_subjects=("industry:pharma",),
            ),
            "5678": GenerationSubject(scope="stock", model_ids=("company_financials",)),
            "industry:pharma": GenerationSubject(scope="sector", model_ids=("industry_trends",)),
            "market:global": GenerationSubject(scope="market", model_ids=("social_trends",)),
        },
        models=(
            GeneratedModel(
                model_id="company_financials",
                subject="1234",
                calculation_revision="v1",
                payload={
                    "expected_price_date": "2026-10-23",
                    "earnings_price": TypeAdapter(EarningsPrice).dump_python(
                        EarningsPrice(
                            as_of=date(2026, 10, 23),
                            price_date=date(2026, 10, 23),
                            price_yen=100.0,
                            shares_ex_treasury=100.0,
                            market_cap_yen=10000.0,
                            profit_ttm_yen=1000.0,
                            statement_basis="consolidated",
                            profit_components=(),
                            earnings_yield=0.1,
                            unavailable_reason=None,
                        ),
                        mode="json",
                    ),
                    "financial": {"pbr": 0.5, "market_cap": 10000.0},
                },
            ),
            GeneratedModel(
                model_id="market_context",
                subject="1234",
                calculation_revision="v1",
                payload={"master": {"name": "合成企業", "sector_33": "情報・通信業"}},
            ),
        ),
    )
    assert ModelGenerationStore(path).publish(fixed)
    return fixed


def saved_bytes(path, identity):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT payload_gzip FROM screening_selection WHERE selection_id=?", (identity,)
        ).fetchone()[0]


def test_fixed_stock_plan_only_is_published_idempotently_without_changing_history(tmp_path, capsys):
    path = tmp_path / "runs.sqlite"
    fixed = saved_generation(path)
    legacy = ScreeningSelection(
        fixed.generation_id,
        ("1234",),
        EconomicPathSelection(EconomicPathPolicy(method="best-economic-path-v3"), 0, ()),
    )
    store = ScreeningSelectionStore(path)
    assert store.publish("selection-old", legacy)
    old_bytes = saved_bytes(path, "selection-old")
    with sqlite3.connect(path) as connection:
        old_header = connection.execute(
            "SELECT header FROM model_generation WHERE generation_id=?", (fixed.generation_id,)
        ).fetchone()[0]

    arguments = ["--generation-id", fixed.generation_id, "--runs", str(path)]
    assert jobs_main(["screening-selection", *arguments]) == 0
    first = json.loads(capsys.readouterr().out)
    identity = "selection-models-fixed-release-best-economic-path-v4"
    assert first == {
        "selection_id": identity,
        "generation_id": fixed.generation_id,
        "policy": {"method": "best-economic-path-v4", "candidate_limit": 36},
        "stock_count": 2,
        "eligible_count": 1,
        "candidate_count": 1,
        "created": True,
    }
    selected = store.read(identity)
    assert selected is not None
    assert selected.universe_tickers == ("1234", "5678")
    assert selected.result.policy.method == "best-economic-path-v4"
    assert selected.result.tickers == ("1234",)
    assert read_publication(path=path).selection_id == identity

    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out) == {**first, "created": False}
    assert saved_bytes(path, "selection-old") == old_bytes
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT header FROM model_generation WHERE generation_id=?", (fixed.generation_id,)
            ).fetchone()[0]
            == old_header
        )


def test_same_selection_id_with_different_policy_content_is_rejected(tmp_path, capsys):
    path = tmp_path / "runs.sqlite"
    fixed = saved_generation(path)
    identity = "selection-models-fixed-release-best-economic-path-v4"
    store = ScreeningSelectionStore(path)
    conflict = ScreeningSelection(
        fixed.generation_id,
        ("1234", "5678"),
        EconomicPathSelection(EconomicPathPolicy(candidate_limit=35), 0, ()),
    )
    assert store.publish(identity, conflict)
    prior_bytes = saved_bytes(path, identity)
    assert main(["--generation-id", fixed.generation_id, "--runs", str(path)]) == 1
    assert "selection identity has different content" in capsys.readouterr().err
    assert saved_bytes(path, identity) == prior_bytes


def test_generation_without_fixed_stock_plan_is_not_relabelled(tmp_path, capsys):
    path = tmp_path / "runs.sqlite"
    old = generation()
    assert ModelGenerationStore(path).publish(old)
    assert main(["--generation-id", old.generation_id, "--runs", str(path)]) == 1
    assert "no fixed stock subject plan" in capsys.readouterr().err
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM screening_selection").fetchone()[0] == 0


def test_obsolete_runs_schema_reports_operator_error_without_traceback(tmp_path, capsys):
    path = tmp_path / "runs.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE old_runs (id TEXT)")
        connection.execute("PRAGMA user_version = 6")
    assert main(["--generation-id", "models-fixed-release", "--runs", str(path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "screening run store schema is not current" in captured.err
    assert "Traceback" not in captured.err
