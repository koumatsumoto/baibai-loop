import json
from dataclasses import replace
from datetime import UTC, date, datetime, time
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from baibai_engine.data_collection.providers.alpaca import (
    MAX_SYMBOLS,
    BarsCapture,
    BarsResponse,
    DailyBarsQuery,
    capture_daily_bars,
)
from baibai_engine.data_collection.providers.nasdaq_symbols import read_us_symbol_directory
from baibai_engine.machine_analysis.observations.prices.us_sip_coverage import (
    AlpacaSIPDailyBatch,
    inspect_alpaca_sip_daily_batch,
    plan_alpaca_sip_daily_batches,
)
from baibai_engine.machine_analysis.observations.stocks.us_universe import (
    RakutenOrdinaryRow,
    cover_rakuten_us_ordinary_shares,
)

_WED = date(2026, 11, 25)
_HOLIDAY = date(2026, 11, 26)
_FRI = date(2026, 11, 27)
_RECEIVED = datetime(2026, 11, 28, 8, tzinfo=UTC)
_NEW_YORK = ZoneInfo("America/New_York")


def _universe(extra: int = 0) -> tuple[tuple[str, str], ...]:
    names = {
        "GOOG": "Class C Capital Stock",
        "GOOGL": "Class A Common Stock",
        "SELL": "Common Stock",
        "UNKNOWN": "Common Stock",
        "TESTETF": "ETF",
        **{f"US{i:02}": "Common Stock" for i in range(extra)},
    }
    nasdaq = "\n".join(
        [
            (
                "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|"
                "ETF|NextShares"
            ),
            *(
                f"{symbol}|Test Co. - {name}|Q|N|N|100|{'Y' if name == 'ETF' else 'N'}|N"
                for symbol, name in names.items()
            ),
            "File Creation Time: 1127202618:01|||||||",
        ]
    ).encode()
    nyse = (
        b"ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
        b"BRK.B|Test Co. Class B|N|BRK.B|N|100|N|BRKB\n"
        b"File Creation Time: 1127202618:01||||||\n"
    )
    rows = [
        RakutenOrdinaryRow("BRK B", "NYSE", "buyable"),
        RakutenOrdinaryRow("7203", "TSE", "buyable"),
        RakutenOrdinaryRow("JPY", "TSE", "buyable"),
        *(
            RakutenOrdinaryRow(
                symbol,
                "NASDAQ",
                {"SELL": "sell_only", "UNKNOWN": "unknown"}.get(symbol, "buyable"),
            )
            for symbol in names
        ),
    ]
    coverage = cover_rakuten_us_ordinary_shares(
        rows,
        reported_count=len(rows),
        directories=(
            read_us_symbol_directory(nasdaq, file="nasdaqlisted.txt"),
            read_us_symbol_directory(nyse, file="otherlisted.txt"),
        ),
    )
    return tuple(
        (row.venue, row.alpaca_symbol)
        for row in coverage
        if row.state == "eligible" and row.venue is not None and row.alpaca_symbol is not None
    )


def _batch() -> AlpacaSIPDailyBatch:
    (batch,) = plan_alpaca_sip_daily_batches(
        _universe(), session_dates=(_WED, _FRI), adjustment="raw"
    )
    return batch


def _row(day: date) -> dict[str, object]:
    return {
        "t": datetime.combine(day, time.min, _NEW_YORK).isoformat(),
        "o": 10,
        "h": 14,
        "l": 8,
        "c": 12,
        "v": 123,
    }


def _page(groups: dict[str, list[dict[str, object]]], token: str | None = None) -> BarsResponse:
    return BarsResponse(
        200, _RECEIVED, json.dumps({"bars": groups, "next_page_token": token}).encode()
    )


def _capture(query: DailyBarsQuery, *responses: BarsResponse) -> BarsCapture:
    stream = iter(responses)
    return capture_daily_bars(
        query, request_id="fixture-sip-batch", transport=lambda _: next(stream)
    )


def test_batch_plan_covers_every_eligible_master_symbol_without_jp_or_ineligible_leaks():
    universe = _universe(extra=MAX_SYMBOLS * 2)
    plan = plan_alpaca_sip_daily_batches(
        reversed(universe), session_dates=(_FRI, _WED), adjustment="split"
    )
    assert plan == plan_alpaca_sip_daily_batches(
        universe, session_dates=(_WED, _FRI), adjustment="split"
    )
    assert [len(batch.query.symbols) for batch in plan] == [MAX_SYMBOLS, MAX_SYMBOLS, 3]
    symbols = ("BRK.B", "GOOG", "GOOGL", *(f"US{i:02}" for i in range(MAX_SYMBOLS * 2)))
    assert tuple(symbol for b in plan for symbol in b.query.symbols) == symbols
    assert tuple(key for b in plan for key in b.expected) == tuple(
        (symbol, day) for symbol in symbols for day in (_WED, _FRI)
    )
    for batch in plan:
        query = parse_qs(urlsplit(batch.query.url()).query)
        assert query["feed"] == ["sip"]
        assert query["currency"] == ["USD"]
        assert query["asof"] == ["-"]
        assert query["adjustment"] == ["split"]
        assert query["start"] == [_WED.isoformat()]
        assert query["end"] == [_FRI.isoformat()]


def test_complete_paginated_capture_reports_exact_symbol_day_coverage():
    batch = _batch()
    capture = _capture(
        batch.query,
        _page({"GOOGL": [_row(_WED)], "GOOG": [_row(_FRI)]}, "opaque+token"),
        _page({"BRK.B": [_row(_FRI)], "GOOGL": [_row(_WED)]}),
    )
    coverage = inspect_alpaca_sip_daily_batch(batch, capture)
    assert coverage.failure is None
    assert coverage.unverified == ()
    assert coverage.received == (("BRK.B", _FRI), ("GOOG", _FRI), ("GOOGL", _WED))
    assert coverage.missing == (("BRK.B", _WED), ("GOOG", _WED), ("GOOGL", _FRI))
    assert set(coverage.received) | set(coverage.missing) == set(batch.expected)
    assert not set(coverage.received) & set(coverage.missing)
    assert len(coverage.observations[-1].sources) == 2
    for row in coverage.observations:
        assert row.requested_symbol == row.returned_symbol
        assert row.feed == "sip"
        assert row.adjustment == "raw"
        assert row.price_unit == "USD_per_share"
        assert row.price_basis == "vendor_daily_aggregate"


def test_empty_answer_partial_failure_and_no_attempt_are_not_the_same():
    batch = _batch()
    empty = _capture(batch.query, _page({}))
    failed = _capture(
        batch.query, _page({"GOOG": [_row(_FRI)]}, "next"), BarsResponse(403, _RECEIVED, b"{}")
    )
    assert len(failed.pages) == 2
    assert not failed.complete
    for capture, reason in ((empty, None), (failed, "http_status"), (None, "not_attempted")):
        coverage = inspect_alpaca_sip_daily_batch(batch, capture)
        assert coverage.failure == reason
        assert coverage.received == ()
        assert coverage.missing == (batch.expected if reason is None else ())
        assert coverage.unverified == (() if reason is None else batch.expected)


@pytest.mark.parametrize(
    ("symbol", "day", "reason"),
    [
        ("7203", _FRI, "scope_mismatch"),
        ("SELL", _FRI, "scope_mismatch"),
        ("GOOG", _HOLIDAY, "unexpected_session"),
        ("GOOG", date(2026, 11, 24), "invalid_capture"),
    ],
)
def test_unexpected_native_symbol_or_day_cannot_supply_coverage(symbol, day, reason):
    batch = _batch()
    capture = _capture(batch.query, _page({symbol: [_row(day)]}))
    coverage = inspect_alpaca_sip_daily_batch(batch, capture)
    assert coverage.failure == reason
    assert coverage.received == ()
    assert coverage.missing == ()
    assert coverage.unverified == batch.expected


def test_other_feed_adjustment_or_batch_are_not_substitutes():
    batches = plan_alpaca_sip_daily_batches(
        _universe(extra=MAX_SYMBOLS), session_dates=(_WED, _FRI), adjustment="raw"
    )
    first, second = batches
    for other_query in (
        replace(first.query, feed="iex"),
        replace(first.query, symbol_asof=_FRI),
        replace(first.query, adjustment="split"),
        second.query,
    ):
        capture = _capture(other_query, _page({other_query.symbols[0]: [_row(_FRI)]}))
        coverage = inspect_alpaca_sip_daily_batch(first, capture)
        assert coverage.failure == "wrong_query"
        assert coverage.missing == ()
        assert coverage.unverified == first.expected


def test_a_failed_batch_does_not_erase_valid_coverage_of_another_batch():
    first, second = plan_alpaca_sip_daily_batches(
        _universe(extra=MAX_SYMBOLS), session_dates=(_FRI,), adjustment="raw"
    )
    failed = inspect_alpaca_sip_daily_batch(
        first, _capture(first.query, BarsResponse(429, _RECEIVED, b"{}"))
    )
    healthy = inspect_alpaca_sip_daily_batch(
        second, _capture(second.query, _page({s: [_row(_FRI)] for s in second.query.symbols}))
    )
    assert failed.unverified == first.expected
    assert healthy.failure is None
    assert healthy.received == second.expected
    assert healthy.missing == healthy.unverified == ()


def test_a_partial_chain_is_not_valid_just_because_its_failure_was_removed():
    batch = _batch()
    failed = _capture(
        batch.query, _page({"GOOG": [_row(_FRI)]}, "next"), BarsResponse(403, _RECEIVED, b"{}")
    )
    claimed_complete = replace(failed, failure=None, pages=failed.pages[:1])
    assert claimed_complete.complete
    coverage = inspect_alpaca_sip_daily_batch(batch, claimed_complete)
    assert coverage.failure == "invalid_capture"
    assert coverage.missing == ()
    assert coverage.unverified == batch.expected


def test_no_calendar_or_no_qualified_symbols_does_not_create_an_empty_success():
    universe = _universe()
    for days in ((), (_WED, _WED)):
        with pytest.raises(ValueError, match="distinct US session dates"):
            plan_alpaca_sip_daily_batches(universe, session_dates=days, adjustment="raw")
    with pytest.raises(ValueError, match="no eligible US symbols"):
        plan_alpaca_sip_daily_batches((), session_dates=(_FRI,), adjustment="raw")
    with pytest.raises(ValueError, match="ambiguous or unqualified"):
        plan_alpaca_sip_daily_batches((("XTKS", "7203"),), session_dates=(_FRI,), adjustment="raw")
    with pytest.raises(ValueError, match="ambiguous or unqualified"):
        plan_alpaca_sip_daily_batches(universe + universe, session_dates=(_FRI,), adjustment="raw")


def test_batch_rejects_iex_and_dated_vendor_symbol_mapping_at_construction():
    batch = _batch()
    for wrong in (replace(batch.query, feed="iex"), replace(batch.query, symbol_asof=_FRI)):
        with pytest.raises(ValueError, match="exact-symbol SIP"):
            replace(batch, query=wrong)


def test_batch_session_scope_cannot_disagree_with_the_planned_query():
    batch = _batch()
    for days in ((), (_FRI, _WED), (_WED,), (_WED, _WED, _FRI)):
        with pytest.raises(ValueError, match="distinct US session dates matching the query"):
            replace(batch, session_dates=days)


@pytest.mark.parametrize("adjustment", ["raw", "split"])
def test_zero_native_volume_does_not_count_as_received_coverage(adjustment):
    batch = replace(_batch(), query=replace(_batch().query, adjustment=adjustment))
    capture = _capture(
        batch.query, _page({"GOOG": [_row(_FRI)], "GOOGL": [{**_row(_FRI), "v": 0}]})
    )
    coverage = inspect_alpaca_sip_daily_batch(batch, capture)
    if adjustment == "raw":
        assert coverage.failure == "invalid_capture"
        assert coverage.received == coverage.missing == ()
        assert coverage.unverified == batch.expected
    else:
        assert coverage.failure is None
        assert coverage.received == (("GOOG", _FRI), ("GOOGL", _FRI))
        assert coverage.unverified == ()
