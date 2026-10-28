from contextlib import closing
from datetime import UTC, date, datetime
import sqlite3

import pytest

from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.screening.policies.two_view_inputs import select_generation
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.machine_analysis.screening.policies.two_views import TwoViewPolicy
from baibai_jobs.maintenance.model_cache import prune_model_cache
from tests.model_generation_fixtures import generation


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
    assert set(prune_model_cache(path, keep=2, preserve=("older",))) == {"c", "bare"}
    assert store.read("a") is not None
    assert store.read("b") is not None
    assert store.read("older") is not None
    assert policy_store.read("c") is None
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert prune_model_cache(path, keep=2, preserve=("older",)) == ()


def test_cache_retention_keeps_a_bare_generation_when_capacity_is_available(tmp_path):
    path = tmp_path / "runs"
    store = ModelGenerationStore(path)
    store.publish(generation())
    assert prune_model_cache(path) == ()
    with pytest.raises(ValueError, match="positive"):
        prune_model_cache(path, keep=0)


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
        prune_model_cache(path, keep=1)
    assert len(store.read("older").models) == len(generation().models)
    assert policies.read("older") is not None
    assert store.read("current") is not None
