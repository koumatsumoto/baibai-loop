"""候補政策の保存・読取契約。モデル生成の保存testとは責務を分ける。"""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

import pytest
from tests.helpers.model_generation import empty_selection, generation

from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.two_views import (
    CandidateView,
    RankedCompany,
    TwoViewPolicy,
    ViewSelection,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore


def test_selection_round_trip_preserves_policy_universe_and_unrounded_result(tmp_path, monkeypatch):
    path = tmp_path / "runs.sqlite"
    original = generation()
    ModelGenerationStore(path).publish(original)
    selected = ScreeningSelection(
        original.generation_id,
        ("1234", "5678"),
        ViewSelection(
            TwoViewPolicy(earnings_limit=2, combined_limit=2),
            (
                CandidateView("earnings-price", 2, 1, (RankedCompany("1234", 1, 0.100049),)),
                CandidateView("earnings-price-response", 2, 0, ()),
            ),
        ),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("storage must not execute candidate policy")

    monkeypatch.setattr(
        "baibai_engine.machine_analysis.screening.policies.economic_path_selection.select_saved_economic_generation",
        forbidden,
    )
    monkeypatch.setattr(
        "baibai_engine.machine_analysis.screening.policies.economic_paths.select_economic_paths",
        forbidden,
    )
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
    selected = empty_selection(generation().generation_id)
    store = ScreeningSelectionStore(tmp_path / "runs.sqlite")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        store.publish("selection-a", selected)
    assert store.read("selection-a") is None


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
            empty_selection(source.generation_id, candidate_limit=limit),
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
    assert latest.selection.result.policy.candidate_limit == 2
    assert read_publication(path=path, before_as_of=current.as_of).selection_id == "late-backfill"
    first = read_publication(path=path, selection_id="z-first")
    store.publish("z-first", first.selection)
    assert read_publication(path=path, selection_id="z-first").published_at == first.published_at
    assert read_publication(path=path).selection_id == "a-second"
    assert read_publication(path=path, selection_id="missing") is None
