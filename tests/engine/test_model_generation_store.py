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
    connect_read_only,
    initialize_run_store,
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
    company = store.read_subjects(original.generation_id, subjects=("1234",))
    assert {model.subject for model in company.models} == {"1234"}
    assert company.inputs == original.inputs
    combined = store.read_subjects(original.generation_id, subjects=("1234", "market"))
    assert combined.models == original.models
    assert combined.subjects == ("1234", "market")
    assert store.read_subjects(original.generation_id, subjects=()).models == ()


def test_partial_input_cannot_be_mistaken_for_a_complete_screening_generation(tmp_path):
    store = ModelGenerationStore(tmp_path / "runs.sqlite")
    first = generation()
    second_company = GeneratedModel(
        model_id="company_valuation",
        subject="5678",
        calculation_revision="v1",
        payload={"earnings_price": {"earnings_yield": 0.2}},
    )
    full = ModelGeneration.model_validate(
        {**first.model_dump(), "models": (*first.models, second_company)}
    )
    store.publish(full)
    policy = TwoViewPolicy(earnings_limit=1, combined_limit=0)
    assert select_generation(full, tickers=("1234", "5678"), policy=policy).result.tickers == (
        "5678",
    )
    partial = store.read_subjects(full.generation_id, subjects=("1234",))
    with pytest.raises(TypeError, match="complete model generation"):
        select_generation(partial, tickers=("1234", "5678"), policy=policy)
    with pytest.raises(TypeError, match="complete model generation"):
        store.publish(partial)
    assert store.read(full.generation_id) == full


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
    with pytest.raises(ValueError, match="Out of range float"):
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


def test_schema6_is_rejected_without_changing_existing_data(tmp_path):
    path = tmp_path / "runs.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(SCHEMA_SQL.removesuffix(GENERATION_SCHEMA_SQL))
        connection.execute("PRAGMA user_version=6")
        connection.execute(
            "INSERT INTO screening_run VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "old",
                "old",
                "2026-10-23",
                "2026-10-23",
                "2026-10-23T00:00:00Z",
                0,
                None,
                "old",
                "{}",
            ),
        )
        connection.commit()
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="obsolete schema"):
        initialize_run_store(path)
    with pytest.raises(RuntimeError, match="schema is not current"):
        connect_read_only(path)
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT payload FROM screening_run").fetchone()[0] == "{}"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert (
            connection.execute(
                "SELECT name FROM sqlite_schema WHERE name = 'model_generation'"
            ).fetchone()
            is None
        )


def test_publication_order_prefers_asof_then_real_publication_and_preserves_retry(tmp_path):
    from baibai_engine.machine_analysis.screening.selection_query import read_publication

    path = tmp_path / "runs"
    models = ModelGenerationStore(path)
    store = ScreeningSelectionStore(path)
    current = generation()
    older = current.model_copy(update={"generation_id": "older", "as_of": date(2026, 10, 22)})
    models.publish(current)
    models.publish(older)
    for name, source, limit in (
        ("z-first", current, 1),
        ("a-second", current, 2),
        ("late-backfill", older, 1),
    ):
        store.publish(
            name,
            select_generation(
                source,
                tickers=("1234",),
                policy=TwoViewPolicy(earnings_limit=limit, combined_limit=0),
            ),
        )
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executemany(
            "UPDATE screening_selection SET published_at=? WHERE selection_id=?",
            [
                ("2026-10-25T09:00:00+00:00", "z-first"),
                ("2026-10-25T18:00:01+09:00", "a-second"),
                ("2026-10-26T00:00:00+00:00", "late-backfill"),
            ],
        )
    latest = read_publication(path=path)
    assert latest.selection_id == "a-second"
    cutoff = datetime(2026, 10, 25, 9, tzinfo=UTC)
    assert read_publication(path=path, known_at=cutoff).selection_id == "z-first"
    assert read_publication(path=path, selection_id="a-second", known_at=cutoff) is None
    assert latest.selection.result.policy.earnings_limit == 2
    assert read_publication(path=path, before_as_of=current.as_of).selection_id == "late-backfill"
    first = read_publication(path=path, selection_id="z-first")
    store.publish("z-first", first.selection)
    assert read_publication(path=path, selection_id="z-first").published_at == first.published_at
    assert read_publication(path=path).selection_id == "a-second"
    assert read_publication(path=path, selection_id="missing") is None


def test_cache_retention_uses_policy_publication_and_preserves_manual_generation(tmp_path):
    path = tmp_path / "runs"
    store = ModelGenerationStore(path)
    policy_store = ScreeningSelectionStore(path)
    policy = TwoViewPolicy(earnings_limit=1, combined_limit=0)
    for name, day in (("older", 20), ("a", 23), ("b", 23), ("c", 23), ("bare", 24)):
        item = generation().model_copy(
            update={
                "generation_id": name,
                "as_of": date(2026, 10, day),
                "evaluation_time": datetime(
                    2026, 10, 25, {"a": 1, "b": 2, "c": 3}.get(name, 4), tzinfo=UTC
                ),
            }
        )
        store.publish(item)
        if name != "bare":
            policy_store.publish(name, select_generation(item, tickers=("1234",), policy=policy))
    with closing(sqlite3.connect(path)) as connection, connection:
        for name, hour in (("a", 3), ("b", 2), ("c", 1), ("older", 4)):
            connection.execute(
                "UPDATE screening_selection SET published_at=? WHERE selection_id=?",
                (f"2026-10-25T{hour:02}:00:00+00:00", name),
            )
    policy_store.publish(
        "a-repeated-policy", select_generation(store.read("a"), tickers=("1234",), policy=policy)
    )
    # Multiple policies for one generation count as one retained generation.
    # A newer bare header cannot displace the operative policy publication.
    assert set(store.prune(keep=2, preserve=("older",))) == {"c", "bare"}
    assert store.read("a") is not None
    assert store.read("b") is not None
    assert store.read("older") is not None
    assert policy_store.read("c") is None
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.prune(keep=2, preserve=("older",)) == ()


def test_cache_retention_keeps_a_bare_generation_when_capacity_is_available(tmp_path):
    store = ModelGenerationStore(tmp_path / "runs")
    store.publish(generation())
    assert store.prune() == ()
    with pytest.raises(ValueError, match="positive"):
        store.prune(keep=0)


def test_cache_retention_rolls_back_policy_and_model_deletion_together(tmp_path):
    path = tmp_path / "runs"
    store = ModelGenerationStore(path)
    policies = ScreeningSelectionStore(path)
    policy = TwoViewPolicy(earnings_limit=1, combined_limit=0)
    for name, day in (("older", 20), ("current", 23)):
        item = generation().model_copy(update={"generation_id": name, "as_of": date(2026, 10, day)})
        store.publish(item)
        policies.publish(name, select_generation(item, tickers=("1234",), policy=policy))
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER stop_cache_delete BEFORE DELETE ON model_generation "
            "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        store.prune(keep=1)
    assert len(store.read("older").models) == len(generation().models)
    assert policies.read("older") is not None
    assert store.read("current") is not None
