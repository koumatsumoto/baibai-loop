"""Daily observations compare published method identities, not file names."""

from contextlib import closing
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from baibai_engine.storage.observations.schema import open_connection
from baibai_web.readmodel.builders import build_daily_delta
from baibai_web.sources.db_sources import DbMarketPriceSource
from baibai_web.sources.types import ScreeningRunRecord


def _run(day: int) -> ScreeningRunRecord:
    return ScreeningRunRecord(
        run_id=f"run-{day}",
        as_of=date(2026, 9, day),
        generated_at=datetime(2026, 9, day, 18, tzinfo=ZoneInfo("Asia/Tokyo")),
        universe_size=3,
        run_revision_id=f"revision-{day}",
        rules_ref="rules.yaml",
        screening_rules_hash="screening-v1",
        er_model_version="er-v1",
        candidate_discovery={"method": {"method_hash": "discovery-v1"}},
        rows=(),
    )


def _candidate_fixture(
    tickers: list[str], er: float, method_hash: str | None = "discovery-v1"
) -> dict:
    return {
        "method_hash": method_hash,
        "rows": [
            {
                "ticker": ticker,
                "name": ticker,
                "metrics": {"er_annual": er},
                "candidate": {
                    "common_eligible": True,
                    "normalized_per_3fy_sector_gap": None,
                    "reasons": [
                        {
                            "valuation_approach_id": "current-earnings-power",
                            "valuation_method_id": "method-v1",
                            "rank": rank,
                        }
                    ],
                },
            }
            for rank, ticker in enumerate(tickers, start=1)
        ],
    }


def _sources(
    latest=None, previous=None, current_candidate_fixture=None, previous_candidate_fixture=None
):
    screening = Mock()
    current = (
        _candidate_fixture(["1003", "1002", "1001"], 0.2)
        if current_candidate_fixture is None
        else current_candidate_fixture
    )
    earlier = (
        _candidate_fixture(["1004", "1001"], 0.1)
        if previous_candidate_fixture is None
        else previous_candidate_fixture
    )
    screening.latest_run.return_value = replace(
        latest or _run(4),
        rows=tuple(current["rows"]),
        candidate_discovery={"method": {"method_hash": current["method_hash"]}},
    )
    screening.previous_run.return_value = replace(
        previous or _run(3),
        rows=tuple(earlier["rows"]),
        candidate_discovery={"method": {"method_hash": earlier["method_hash"]}},
    )
    ledger = Mock()
    ledger.exists.return_value = False
    market = Mock()
    market.exists.return_value = True
    market.disclosures_after.return_value = []
    return screening, ledger, Mock(), market


@pytest.mark.parametrize("change", ["none", "path", "screening", "discovery", "er"])
def test_daily_delta_compares_method_identity_by_output(change: str) -> None:
    latest = _run(4)
    if change == "path":
        latest = replace(latest, rules_ref="renamed.yaml")
    elif change == "screening":
        latest = replace(latest, screening_rules_hash="screening-v2")
    elif change == "er":
        latest = replace(latest, er_model_version="er-v2")
    current = _candidate_fixture(
        ["1003", "1002", "1001"], 0.2, "discovery-v2" if change == "discovery" else "discovery-v1"
    )
    sources = _sources(latest=latest, current_candidate_fixture=current)
    view = build_daily_delta(*sources)
    changed = change in {"screening", "discovery"}
    assert view.method_changed is changed
    assert [row.ticker for row in view.entered] == ([] if changed else ["1002", "1003"])
    assert [row.ticker for row in view.exited] == ([] if changed else ["1004"])
    assert [row.ticker for row in view.er_moves] == ([] if changed or change == "er" else ["1001"])
    assert view.er_moves_total == (0 if changed or change == "er" else 1)
    assert ("candidate_estimate" in view.unavailable) is (change == "er")
    assert sources[0].latest_run.call_count == 1
    assert sources[0].previous_run.call_count == 1


@pytest.mark.parametrize("side", ["latest", "previous"])
@pytest.mark.parametrize("identity", ["screening", "discovery", "er"])
def test_missing_identity_is_not_treated_as_matching(side: str, identity: str) -> None:
    latest, previous = _run(4), _run(3)
    current, earlier = _candidate_fixture(["1002", "1001"], 0.2), _candidate_fixture(["1001"], 0.1)
    if identity == "discovery":
        (current if side == "latest" else earlier)["method_hash"] = None
    else:
        field = "screening_rules_hash" if identity == "screening" else "er_model_version"
        if side == "latest":
            latest = replace(latest, **{field: None})
        else:
            previous = replace(previous, **{field: None})
    if identity == "discovery":
        with pytest.raises(ValueError, match="candidate method"):
            build_daily_delta(*_sources(latest, previous, current, earlier))
        return
    view = build_daily_delta(*_sources(latest, previous, current, earlier))
    assert not view.method_changed
    assert view.er_moves == []
    assert view.er_moves_total == 0
    assert [item.ticker for item in view.entered] == (["1002"] if identity == "er" else [])
    assert ("candidate_estimate" if identity == "er" else "candidates") in view.unavailable


@pytest.mark.parametrize("missing", ["latest", "previous", "empty"])
def test_absent_comparison_and_empty_candidate_fixture_are_distinct(missing: str) -> None:
    sources = _sources(
        current_candidate_fixture=_candidate_fixture([], 0.2),
        previous_candidate_fixture=_candidate_fixture([], 0.1),
    )
    if missing == "latest":
        sources[0].latest_run.return_value = None
    elif missing == "previous":
        sources[0].previous_run.return_value = None
    view = build_daily_delta(*sources)
    expected = {
        "latest": "screening_run",
        "previous": "previous_screening_run",
    }.get(missing)
    assert view.entered == view.exited == view.er_moves == []
    assert not view.method_changed
    if expected:
        assert expected in view.unavailable
    else:
        assert view.unavailable == ["holdings"]


def test_method_change_does_not_skip_holdings(mocker) -> None:
    sources = _sources(latest=replace(_run(4), screening_rules_hash="changed"))
    sources[1].exists.return_value = True
    holding_delta = mocker.patch(
        "baibai_web.readmodel.builders.daily_delta._holding_deltas", return_value=([], 3)
    )
    view = build_daily_delta(*sources)
    assert view.method_changed
    assert view.holdings_without_price == 3
    holding_delta.assert_called_once()


@pytest.mark.parametrize("side", ["current", "previous", "both"])
@pytest.mark.parametrize("shape", ["null", "absent"])
@pytest.mark.parametrize("comparable", [False, True])
def test_partial_estimate_missing_is_visible_without_hiding_comparable_movers(
    side: str, shape: str, comparable: bool
) -> None:
    tickers = ["1001", "1002"] if comparable else ["1001"]
    current, previous = _candidate_fixture(tickers, 0.2), _candidate_fixture(tickers, 0.1)
    for name, publications in [("current", current), ("previous", previous)]:
        if side not in {name, "both"}:
            continue
        expected = publications["rows"][0]["metrics"]
        if shape == "null":
            expected["er_annual"] = None
        else:
            del expected["er_annual"]
    view = build_daily_delta(
        *_sources(current_candidate_fixture=current, previous_candidate_fixture=previous)
    )
    assert "candidate_estimate" in view.unavailable
    assert [row.ticker for row in view.er_moves] == (["1002"] if comparable else [])
    assert view.er_moves_total == int(comparable)
    assert view.entered == view.exited == []


def test_estimate_missing_outside_shared_tickers_does_not_prevent_comparison() -> None:
    current, previous = (
        _candidate_fixture(["1001", "1002"], 0.2),
        _candidate_fixture(["1001", "1003"], 0.1),
    )
    for publications in [current, previous]:
        publications["rows"][1]["metrics"]["er_annual"] = None
    view = build_daily_delta(
        *_sources(current_candidate_fixture=current, previous_candidate_fixture=previous)
    )
    assert "candidate_estimate" not in view.unavailable
    assert [row.ticker for row in view.entered] == ["1002"]
    assert [row.ticker for row in view.exited] == ["1003"]
    assert [row.ticker for row in view.er_moves] == ["1001"]


def test_disjoint_candidate_fixtures_have_no_unmeasured_shared_estimates() -> None:
    current, previous = _candidate_fixture(["1001"], 0.2), _candidate_fixture(["1002"], 0.1)
    for publications in [current, previous]:
        publications["rows"][0]["metrics"]["er_annual"] = None
    view = build_daily_delta(
        *_sources(current_candidate_fixture=current, previous_candidate_fixture=previous)
    )
    assert "candidate_estimate" not in view.unavailable
    assert view.er_moves_total == 0
    assert [row.ticker for row in view.entered] == ["1001"]
    assert [row.ticker for row in view.exited] == ["1002"]


def test_mover_total_retains_rows_beyond_the_display_cap() -> None:
    tickers = [str(1000 + index) for index in range(20)]
    view = build_daily_delta(
        *_sources(
            current_candidate_fixture=_candidate_fixture(tickers, 0.2),
            previous_candidate_fixture=_candidate_fixture(tickers, 0.1),
        )
    )
    assert view.er_moves_total == 20
    assert len(view.er_moves) == 5


def test_disclosure_context_stays_inside_the_compared_runs(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as connection, connection:
        connection.executemany(
            "INSERT INTO jquants_fin_summaries "
            "(ticker, disclosed_at, disclosure_number, document_payload) VALUES (?, ?, ?, ?)",
            [
                ("1002", "2026-09-04", "inside", "{}"),
                ("1002", "2026-09-05", "newer", "{}"),
                ("1003", "2026-09-05", "after-run", "{}"),
                ("1004", "2026-09-03", "previous-run", "{}"),
            ],
        )
    screening, ledger, research, _ = _sources()
    market = DbMarketPriceSource(path)
    view = build_daily_delta(screening, ledger, research, market)
    assert {row.ticker: row.disclosed_since_previous for row in view.entered} == {
        "1002": True,
        "1003": False,
    }
    assert [(row.ticker, row.disclosed_since_previous) for row in view.exited] == [("1004", False)]
    assert market.disclosures_after(
        ["1002", "1003", "1004"], after=date(2026, 9, 3), through_date=date(2026, 9, 4)
    ) == {"1002": date(2026, 9, 4)}


@pytest.mark.parametrize(
    ("previous", "current", "included"),
    [
        (0.1004, 0.13, False),
        (0.13, 0.1004, False),
        (0.10, 0.13, True),
        (0.10, 0.07, True),
        (0.10, 0.13001, True),
        (0.10, 0.06999, True),
    ],
)
def test_estimate_mover_threshold_uses_unrounded_change(previous, current, included):
    view = build_daily_delta(
        *_sources(
            current_candidate_fixture=_candidate_fixture(["1001"], current),
            previous_candidate_fixture=_candidate_fixture(["1001"], previous),
        )
    )
    assert view.er_moves_total == int(included)
    assert len(view.er_moves) == int(included)
    if included:
        assert abs(view.er_moves[0].change_pp) == 3.0


def test_estimate_mover_display_cap_ranks_unrounded_changes():
    tickers = [str(1000 + index) for index in range(6)]
    current = _candidate_fixture(tickers, 0.13)
    for index, row in enumerate(current["rows"]):
        row["metrics"]["er_annual"] = 0.13001 + index * 0.00001
    view = build_daily_delta(
        *_sources(
            current_candidate_fixture=current,
            previous_candidate_fixture=_candidate_fixture(tickers, 0.10),
        )
    )
    assert view.er_moves_total == 6
    assert [row.ticker for row in view.er_moves] == list(reversed(tickers[1:]))
    assert all(row.change_pp == 3.0 for row in view.er_moves)
