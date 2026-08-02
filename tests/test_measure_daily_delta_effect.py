from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from tools.measure_daily_delta_effect import (
    build_candidate_measurement,
    build_holding_measurement,
    load_longlist_history,
)


def _history(day: str, selection_id: str | None, tickers: list[str]) -> dict[str, object]:
    return {
        "kind": "daily-longlist-membership",
        "schema_version": 1,
        "as_of": day,
        "selection_status": "available" if selection_id is not None else "selection_missing",
        "selection_id": selection_id,
        "members": [{"ticker": ticker, "rank": rank + 1} for rank, ticker in enumerate(tickers)],
    }


def _shortlist(
    day: str,
    selection_id: str,
    entries: list[tuple[str, str]],
    *,
    published: str | None = None,
) -> dict[str, object]:
    return {
        "shortlist_id": f"shortlist-{day}",
        "selection_id": selection_id,
        "as_of": day,
        "published_at": f"{published or day}T09:00:00+09:00",
        "entries": [{"ticker": ticker, "decision": decision} for ticker, decision in entries],
    }


def _thesis(ticker: str, *, published: str, fv: float) -> dict[str, object]:
    return {
        "thesis_id": f"thesis-{ticker}",
        "ticker": ticker,
        "as_of": published,
        "published_at": f"{published}T08:00:00+09:00",
        "payload": {"estimates": {"current_fair_value_yen": fv}},
    }


def _bar(day: str, close: float, factor: float = 1.0) -> dict[str, object]:
    return {"traded_at": day, "close": close, "adjustment_factor": factor}


def test_candidate_latency_separates_exact_first_seen_from_left_censoring() -> None:
    result = build_candidate_measurement(
        longlist_history=[
            _history("2026-07-01", "selection-1", ["1111"]),
            _history("2026-07-02", "selection-2", ["1111", "2222"]),
        ],
        expected_dates=[date(2026, 7, 1), date(2026, 7, 2)],
        shortlists=[
            _shortlist(
                "2026-07-01",
                "selection-1",
                [("2222", "rejected")],
                published="2026-07-02",
            ),
            _shortlist(
                "2026-07-03",
                "selection-2",
                [("1111", "selected"), ("2222", "rejected")],
            ),
        ],
        asof=date(2026, 7, 3),
    )

    assert result["pool_unique_ticker_count"] == 2
    assert result["evaluated_ticker_count"] == 2
    assert result["capture_rate_pct"] == 100.0
    assert result["exact_first_seen_count"] == 1
    assert result["left_censored_first_seen_count"] == 1
    assert result["exact_delay_distribution"] == {
        "count": 1,
        "min_days": 1,
        "median_days": 1,
        "max_days": 1,
        "buckets": {"0d": 0, "1d": 1, "2_3d": 0, "4_7d": 0, "8d_plus": 0},
    }
    by_ticker = {row["ticker"]: row for row in result["tickers"]}
    assert by_ticker["1111"]["delay_days"] is None
    assert by_ticker["1111"]["delay_lower_bound_days"] == 2
    assert by_ticker["2222"]["delay_days"] == 1


def test_missing_longlist_snapshot_keeps_later_first_seen_censored() -> None:
    result = build_candidate_measurement(
        longlist_history=[
            _history("2026-07-01", None, []),
            _history("2026-07-02", "selection-2", ["2222"]),
        ],
        expected_dates=[date(2026, 7, 1), date(2026, 7, 2)],
        shortlists=[],
        asof=date(2026, 7, 3),
    )

    assert result["coverage"]["missing_selection_snapshot_count"] == 1
    assert result["exact_first_seen_count"] == 0
    assert result["tickers"][0]["first_seen_status"] == "left_censored"
    assert result["tickers"][0]["evaluation_status"] == "right_censored"


def test_empty_longlist_is_a_complete_daily_observation() -> None:
    result = build_candidate_measurement(
        longlist_history=[
            _history("2026-07-01", "selection-1", []),
            _history("2026-07-02", "selection-2", ["2222"]),
        ],
        expected_dates=[date(2026, 7, 1), date(2026, 7, 2)],
        shortlists=[],
        asof=date(2026, 7, 3),
    )

    assert result["coverage"]["complete_longlist_snapshot_count"] == 2
    assert result["coverage"]["missing_selection_snapshot_count"] == 0
    assert result["tickers"][0]["first_seen_status"] == "exact"


def test_missing_history_record_keeps_later_first_seen_censored() -> None:
    result = build_candidate_measurement(
        longlist_history=[
            _history("2026-07-01", "selection-1", []),
            _history("2026-07-03", "selection-3", ["2222"]),
        ],
        expected_dates=[date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)],
        shortlists=[],
        asof=date(2026, 7, 3),
    )

    assert result["coverage"]["missing_history_record_count"] == 1
    assert result["snapshots"][1]["status"] == "record_missing"
    assert result["tickers"][0]["first_seen_status"] == "left_censored"


def test_longlist_history_loader_rejects_filename_asof_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "2026-07-01.json"
    path.write_text(
        '{"kind":"daily-longlist-membership","schema_version":1,'
        '"as_of":"2026-07-02","selection_status":"selection_missing",'
        '"selection_id":null,"members":[]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="filename and as_of differ"):
        load_longlist_history(tmp_path)


def test_holding_latency_starts_at_fv_reach_and_ignores_an_earlier_review() -> None:
    result = build_holding_measurement(
        open_tickers=["1111", "2222", "3333"],
        ledger_asof=datetime.fromisoformat("2026-07-01T09:00:00+09:00"),
        theses=[
            _thesis("1111", published="2026-07-01", fv=100),
            _thesis("2222", published="2026-07-01", fv=100),
        ],
        reviews=[
            {
                "holding_review_id": "review-before",
                "ticker": "1111",
                "as_of": "2026-07-01",
            },
            {
                "holding_review_id": "review-after",
                "ticker": "1111",
                "as_of": "2026-07-03",
            },
        ],
        bars_by_ticker={
            "1111": [_bar("2026-07-01", 90), _bar("2026-07-02", 110)],
            "2222": [_bar("2026-07-02", 110, factor=0.5)],
        },
        asof=date(2026, 7, 4),
    )

    assert result["open_holding_count"] == 3
    assert result["fv_trigger_count"] == 1
    assert result["reviewed_after_trigger_count"] == 1
    assert result["capture_rate_pct"] == 100.0
    by_ticker = {row["ticker"]: row for row in result["holdings"]}
    assert by_ticker["1111"]["fv_reached_date"] == "2026-07-02"
    assert by_ticker["1111"]["holding_review_id"] == "review-after"
    assert by_ticker["1111"]["delay_days"] == 1
    assert by_ticker["2222"]["status"] == "corporate_action_unresolved"
    assert by_ticker["3333"]["status"] == "thesis_missing"


def test_reached_holding_without_review_is_right_censored() -> None:
    result = build_holding_measurement(
        open_tickers=["1111"],
        ledger_asof=datetime.fromisoformat("2026-07-01T09:00:00+09:00"),
        theses=[_thesis("1111", published="2026-07-01", fv=100)],
        reviews=[],
        bars_by_ticker={"1111": [_bar("2026-07-02", 101)]},
        asof=date(2026, 7, 5),
    )

    assert result["fv_trigger_count"] == 1
    assert result["reviewed_after_trigger_count"] == 0
    assert result["capture_rate_pct"] == 0.0
    assert result["holdings"][0]["status"] == "triggered_unreviewed"
    assert result["holdings"][0]["right_censored_days"] == 3
