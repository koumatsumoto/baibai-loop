"""候補政策の保存・読取契約。モデル生成の保存testとは責務を分ける。"""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

import pytest
from tests.helpers.model_generation import empty_selection, generation, publish_generation

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
    publish_generation(original, path=path)
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
    store = ScreeningSelectionStore(path)
    current = generation()
    older = current.model_copy(update={"generation_id": "older", "as_of": date(2026, 10, 22)})
    publish_generation(current, path=path)
    publish_generation(older, path=path)
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


def test_pricing_gaps_preserve_overlapping_failures_without_reselection(monkeypatch):
    from contextlib import contextmanager
    from copy import deepcopy

    from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
    from baibai_engine.machine_analysis.screening import selection_query
    from baibai_engine.machine_analysis.screening.policies import economic_path_selection
    from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import (
        pricing_input_failures,
    )

    fixed = generation()
    valid = {
        "expected_price_date": "2026-11-02",
        "earnings_price": {
            "price_date": "2026-11-02",
            "price_yen": 100.0,
            "shares_ex_treasury": 1000.0,
            "market_cap_yen": 100000.0,
            "unavailable_reason": None,
        },
    }
    payloads = {str(ticker): deepcopy(valid) for ticker in range(1001, 1009)}
    payloads["1002"]["earnings_price"]["unavailable_reason"] = "unavailable_annual_profit"
    for ticker in ("1003", "1004", "1005"):
        payloads[ticker]["earnings_price"].update(
            shares_ex_treasury=None, market_cap_yen=None, unavailable_reason="missing_capital_basis"
        )
    payloads["1003"]["earnings_price"]["price_date"] = "2026-10-30"
    payloads["1004"].update(
        financial={"market_cap": 500000000.0},
        provider_capital_crosscheck={"provider_market_cap_yen": 500000000.0},
    )
    payloads["1005"]["financial"] = {
        "capital_basis_failure_reason": "indeterminate_treasury_after_issued_observation"
    }
    payloads["1006"]["earnings_price"]["shares_ex_treasury"] = 0.0
    payloads["1008"]["earnings_price"]["price_date"] = "2026-10-30"
    del payloads["1007"]
    before = deepcopy(payloads)
    subjects = tuple(str(ticker) for ticker in range(1001, 1009))
    visits = []

    @contextmanager
    def stream(generation_id, *, subjects, model_ids, path):
        visits.append((generation_id, subjects, model_ids))
        yield (
            fixed,
            (
                (
                    subject,
                    (
                        GeneratedModel(
                            model_id="company_financials",
                            subject=subject,
                            calculation_revision="fixed-v1",
                            payload=payloads[subject],
                        ),
                    ),
                )
                for subject in subjects
                if subject in payloads
            ),
        )

    def no_selection(*args, **kwargs):
        pytest.fail("read-only gap inspection must not project all models or reselect")

    monkeypatch.setattr(selection_query, "stream_generation_subjects", stream)
    monkeypatch.setattr(economic_path_selection, "project_company", no_selection)
    monkeypatch.setattr(economic_path_selection, "select_economic_paths", no_selection)
    selection = empty_selection(fixed.generation_id, tickers=subjects)
    result = selection_query.read_pricing_gaps(selection)
    assert visits == [(fixed.generation_id, subjects, ("company_financials",))]
    assert result["basis"] == "current_economic_path_pricing_gate"
    assert (result["universe_count"], result["valid_count"], result["invalid_count"]) == (8, 2, 6)
    rows = {row["ticker"]: row for row in result["companies"]}
    assert tuple(rows) == tuple(str(ticker) for ticker in range(1003, 1009))
    assert rows["1003"]["failures"] == [
        "invalid_market_cap_yen",
        "invalid_shares_ex_treasury",
        "price_date_mismatch",
    ]
    assert rows["1003"]["earnings_price"] == payloads["1003"]["earnings_price"]
    assert rows["1004"]["earnings_price"]["market_cap_yen"] is None
    assert rows["1005"]["capital_basis_failure_reason"] == (
        "indeterminate_treasury_after_issued_observation"
    )
    assert rows["1006"]["earnings_price"]["shares_ex_treasury"] == 0.0
    assert rows["1007"] == {
        "ticker": "1007",
        "model_revision": None,
        "failures": ["missing_company_financials"],
    }
    assert rows["1008"]["failures"] == ["price_date_mismatch"]
    assert all(rows[ticker]["model_revision"] == "fixed-v1" for ticker in rows if ticker != "1007")
    assert pricing_input_failures({}) == (
        "invalid_market_cap_yen",
        "invalid_price_yen",
        "invalid_shares_ex_treasury",
        "missing_expected_price_date",
    )
    assert payloads == before
