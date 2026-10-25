import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

import pytest

from baibai_engine.machine_analysis.generation_store import (
    GenerationConflictError,
    ModelGenerationStore,
)
from baibai_engine.machine_analysis.model_generation import (
    GeneratedModel,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.screening.model_projection import select_generation
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.machine_analysis.screening.views import TwoViewPolicy
from baibai_engine.storage.machine_analysis import (
    GENERATION_SCHEMA_SQL,
    SCHEMA_SQL,
    initialize_run_store,
    migrate_run_store_v6,
)


def generation(*, value=0.100049):
    return ModelGeneration(
        generation_id="synthetic-generation",
        as_of=date(2026, 10, 23),
        evaluation_time=datetime(2026, 10, 25, tzinfo=UTC),
        inputs={
            "market": InputSnapshot(kind="lake_release", reference="fixed-input", sha256="a" * 64)
        },
        models=(
            GeneratedModel(
                model_id="technical_analysis",
                subject="1234",
                calculation_revision="v1",
                payload={"price_momentum": {"value": None, "reason": "missing_endpoint"}},
            ),
            GeneratedModel(
                model_id="company_valuation",
                subject="1234",
                calculation_revision="v1",
                payload={"earnings_price": {"earnings_yield": value}},
            ),
            GeneratedModel(
                model_id="market_context",
                subject="market",
                calculation_revision="v1",
                payload={"series": [{"value": None, "unit": "percent"}]},
            ),
        ),
    )


def test_generation_round_trip_is_independent_of_candidate_selection(tmp_path):
    store = ModelGenerationStore(tmp_path / "runs.sqlite")
    original = generation()
    assert store.publish(original)
    assert not store.publish(original)
    assert store.read(original.generation_id) == original
    assert store.read("missing") is None
    company = store.read(original.generation_id, subjects=("1234",))
    assert {model.subject for model in company.models} == {"1234"}
    assert company.inputs == original.inputs
    combined = store.read(original.generation_id, subjects=("1234", "market"))
    assert combined == original
    assert store.read(original.generation_id, subjects=()).models == ()


def test_generation_identity_cannot_be_reused_for_new_values_or_new_inputs(tmp_path):
    store = ModelGenerationStore(tmp_path / "runs.sqlite")
    original = generation()
    store.publish(original)
    with pytest.raises(GenerationConflictError, match="model content"):
        store.publish(generation(value=0.100041))
    different_input = original.model_copy(
        update={
            "inputs": {
                "market": InputSnapshot(
                    kind="lake_release", reference="different-input", sha256="b" * 64
                )
            }
        }
    )
    with pytest.raises(GenerationConflictError, match="different content"):
        store.publish(different_input)
    assert store.read(original.generation_id) == original


def test_failed_generation_publication_rolls_back_header_and_models(tmp_path):
    path = tmp_path / "runs.sqlite"
    store = ModelGenerationStore(path)
    with pytest.raises(ValueError):
        store.publish(generation(value=float("nan")))
    assert store.read("synthetic-generation") is None
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT count(*) FROM generated_model").fetchone()[0] == 0


def test_selection_round_trip_preserves_policy_universe_and_unrounded_result(tmp_path, monkeypatch):
    path = tmp_path / "runs.sqlite"
    original = generation()
    ModelGenerationStore(path).publish(original)
    selected = select_generation(
        original, tickers=("1234", "5678"), policy=TwoViewPolicy(earnings_limit=2, combined_limit=2)
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("storage must not execute candidate policy")

    monkeypatch.setattr(
        "baibai_engine.machine_analysis.screening.model_projection.select_generation", forbidden
    )
    monkeypatch.setattr("baibai_engine.machine_analysis.screening.views.select_views", forbidden)
    store = ScreeningSelectionStore(path)
    assert store.publish("selection-a", selected)
    assert not store.publish("selection-a", selected)
    restored = store.read("selection-a")
    assert restored == selected
    assert restored.universe_tickers == ("1234", "5678")
    assert restored.result.views[0].companies[0].ranking_value == 0.100049
    assert restored.result.views[1].companies == ()
    assert store.read("missing") is None


def test_selection_cannot_reference_an_unpublished_generation(tmp_path):
    selected = select_generation(
        generation(), tickers=("1234",), policy=TwoViewPolicy(earnings_limit=1, combined_limit=1)
    )
    store = ScreeningSelectionStore(tmp_path / "runs.sqlite")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        store.publish("selection-a", selected)
    assert store.read("selection-a") is None


def test_schema6_requires_explicit_migration_and_preserves_existing_data(tmp_path):
    path = tmp_path / "runs.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(SCHEMA_SQL.removesuffix(GENERATION_SCHEMA_SQL))
        connection.execute("PRAGMA user_version=6")
        connection.execute(
            "INSERT INTO screening_run VALUES (?,?,?,?,?,?,?,?,?)",
            ("old", "old", "2026-10-23", "2026-10-23", "2026-10-23T00:00:00Z", 0, None, "old", "{}"),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="obsolete schema"):
        initialize_run_store(path)
    assert migrate_run_store_v6(path) == 7
    assert migrate_run_store_v6(path) == 7
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT payload FROM screening_run").fetchone()[0] == "{}"
    assert ModelGenerationStore(path).publish(generation())
