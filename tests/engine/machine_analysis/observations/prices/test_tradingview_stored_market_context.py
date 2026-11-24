from contextlib import closing
from datetime import date, timedelta

import pytest
from tests.helpers.tradingview_market import CLOCK, capture

from baibai_engine.data_collection.providers.tradingview.market_models import MarketToolCapture
from baibai_engine.data_collection.providers.tradingview.market_receipts import (
    read_market_capture,
    retain_market_capture,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.observations.prices.tradingview_market import (
    stored_market_context,
)


def test_stored_quotes_and_bars_keep_separate_clocks_and_delays(tmp_path):
    path = tmp_path / "economic.sqlite"
    with closing(initialize_database(path)) as c:
        retain_market_capture(c, capture(receipt_id="quote"))
        retain_market_capture(
            c, capture(receipt_id="bar", bars=True, received=CLOCK + timedelta(minutes=1))
        )
    value = stored_market_context(path, asof=date(2026, 1, 5), known_at=CLOCK + timedelta(hours=1))
    q = next(x for x in value["quotes"] if x["symbol"] == "CME:SYNTH")
    b = value["bars"][0]
    assert q["delay_seconds"] == 600
    assert q["last_trade_at"] is None
    assert b["last_bar"]["c"] == 11
    assert b["last_bar_at"] != b["received_at"]
    assert b["last_bar_may_change"] is True
    assert value["coverage"] == "partial"  # another requested quote was absent


def test_latest_failed_quote_does_not_revive_older_success(tmp_path):
    path = tmp_path / "economic.sqlite"
    with closing(initialize_database(path)) as c:
        retain_market_capture(c, capture(receipt_id="old"))
        retain_market_capture(
            c, capture(receipt_id="failed", received=CLOCK + timedelta(minutes=1), error=True)
        )
    value = stored_market_context(path, asof=date(2026, 1, 5), known_at=CLOCK + timedelta(hours=1))
    q = next(x for x in value["quotes"] if x["symbol"] == "CME:SYNTH")
    assert q["status"] == "failed"
    assert "fields" not in q
    assert q["receipt_id"] == "failed"


@pytest.mark.parametrize("bars", [False, True])
def test_latest_invalid_payload_keeps_requested_symbols_failed(tmp_path, bars):
    from baibai_engine.data_collection.providers.tradingview.market_models import MarketToolCapture
    from baibai_engine.data_collection.providers.tradingview.market_receipts import (
        read_market_capture,
    )

    path = tmp_path / "economic.sqlite"
    changed = capture(
        receipt_id="new-invalid", bars=bars, received=CLOCK + timedelta(minutes=1)
    ).model_dump(by_alias=True)
    if bars:
        changed["result"]["structuredContent"]["symbol"] = "OTHER:SYNTH"
    else:
        changed["result"]["structuredContent"]["data"] = {}
    invalid = MarketToolCapture.model_validate(changed)
    with closing(initialize_database(path)) as c:
        retain_market_capture(c, capture(receipt_id="old-success", bars=bars))
        retain_market_capture(c, invalid)
        assert (
            read_market_capture(c, invalid.receipt_id, expected_sha256=invalid.digest()) == invalid
        )
    context = stored_market_context(
        path, asof=date(2026, 1, 5), known_at=CLOCK + timedelta(hours=1)
    )
    entries = context["bars" if bars else "quotes"]
    assert {entry["symbol"] for entry in entries} == (
        {"CME:SYNTH"} if bars else {"CME:SYNTH", "TSE:SYNTH"}
    )
    for entry in entries:
        assert entry["status"] == "invalid_payload"
        assert entry["receipt_id"] == invalid.receipt_id
        assert entry["capture_sha256"] == invalid.digest()
        assert entry["received_at"] == invalid.received_at_utc.isoformat()
        assert "fields" not in entry
        assert entry.get("last_bar") is None
    assert context["coverage"] == "partial"
    assert context["unidentified_invalid_receipts"] == 0

    with closing(initialize_database(path)) as c:
        retain_market_capture(
            c,
            capture(receipt_id="recovered", bars=bars, received=CLOCK + timedelta(minutes=2)),
        )
    context = stored_market_context(
        path, asof=date(2026, 1, 5), known_at=CLOCK + timedelta(hours=1)
    )
    latest = next(x for x in context["bars" if bars else "quotes"] if x["symbol"] == "CME:SYNTH")
    assert latest["status"] == "received"
    assert latest["receipt_id"] == "recovered"


@pytest.mark.parametrize(
    ("bars", "field", "value", "status"),
    [
        (False, "close", 10**400, "missing_or_invalid_close"),
        (True, "c", 10**400, "invalid_bar_values"),
        (True, "t", 10**30, "invalid_payload"),
    ],
)
def test_out_of_range_native_numbers_remain_failed_evidence(tmp_path, bars, field, value, status):
    from baibai_engine.data_collection.providers.tradingview.market_models import MarketToolCapture

    path = tmp_path / "economic.sqlite"
    changed = capture(
        receipt_id="out-of-range", bars=bars, received=CLOCK + timedelta(minutes=1)
    ).model_dump(by_alias=True)
    payload = changed["result"]["structuredContent"]
    target = payload["bars"][-1] if bars else payload["data"]["CME:SYNTH"]
    target[field] = value
    with closing(initialize_database(path)) as c:
        retain_market_capture(c, capture(receipt_id="old-success", bars=bars))
        retain_market_capture(c, MarketToolCapture.model_validate(changed))
    context = stored_market_context(
        path, asof=date(2026, 1, 5), known_at=CLOCK + timedelta(hours=1)
    )
    latest = next(x for x in context["bars" if bars else "quotes"] if x["symbol"] == "CME:SYNTH")
    assert latest["status"] == status
    assert latest["receipt_id"] == "out-of-range"
    assert "fields" not in latest
    assert latest.get("last_bar") is None
    assert context["coverage"] == "partial"


def test_receipts_after_cutoff_or_asof_day_are_not_used(tmp_path):
    path = tmp_path / "economic.sqlite"
    with closing(initialize_database(path)) as c:
        retain_market_capture(c, capture(receipt_id="future", received=CLOCK + timedelta(days=1)))
    assert (
        stored_market_context(path, asof=date(2026, 1, 5), known_at=CLOCK + timedelta(days=2))[
            "coverage"
        ]
        == "not_observed"
    )
    assert (
        stored_market_context(path, asof=date(2026, 1, 7), known_at=CLOCK)["coverage"]
        == "not_observed"
    )


def test_same_clock_conflict_is_not_arbitrarily_selected(tmp_path):
    from baibai_engine.data_collection.providers.tradingview.market_models import MarketToolCapture

    path = tmp_path / "economic.sqlite"
    second = capture(receipt_id="second").model_dump(by_alias=True)
    second["result"]["structuredContent"]["data"]["CME:SYNTH"]["close"] = 20
    with closing(initialize_database(path)) as c:
        retain_market_capture(c, capture(receipt_id="first"))
        retain_market_capture(c, MarketToolCapture.model_validate(second))
    value = stored_market_context(path, asof=date(2026, 1, 5), known_at=CLOCK + timedelta(hours=1))
    quotes = {q["symbol"]: q for q in value["quotes"]}
    assert quotes["CME:SYNTH"]["status"] == "ambiguous_same_time"
    assert "fields" not in quotes["CME:SYNTH"]
    assert quotes["TSE:SYNTH"]["status"] == "missing"
    assert value["coverage"] == "partial"


def test_duplicate_equal_values_at_same_clock_are_not_an_ambiguity(tmp_path):
    path = tmp_path / "economic.sqlite"
    with closing(initialize_database(path)) as c:
        retain_market_capture(c, capture(receipt_id="first"))
        retain_market_capture(c, capture(receipt_id="second"))
    value = stored_market_context(path, asof=date(2026, 1, 5), known_at=CLOCK + timedelta(hours=1))
    assert next(q for q in value["quotes"] if q["symbol"] == "CME:SYNTH")["status"] == "received"


def _capture_with_volume(receipt_id, volume):
    value = capture(receipt_id=receipt_id, bars=True).model_dump(by_alias=True)
    value["result"]["structuredContent"]["bars"][-1]["v"] = volume
    return MarketToolCapture.model_validate(value)


@pytest.mark.parametrize(
    ("volume", "invalid"),
    [(True, True), (False, True), ("1", True), (-1, True), (None, False), (0, False), (1.5, False)],
)
def test_optional_volume_cannot_create_a_number_or_erase_priced_bar(tmp_path, volume, invalid):
    path = tmp_path / "economic.sqlite"
    raw = _capture_with_volume("volume", volume)
    quote = capture(receipt_id="quote").model_dump(by_alias=True)
    quote["request_parameters"]["symbols"] = ["CME:SYNTH"]
    quote["result"]["structuredContent"].update(missing=[], missing_count=0)
    with closing(initialize_database(path)) as conn:
        retain_market_capture(conn, raw)
        retain_market_capture(conn, MarketToolCapture.model_validate(quote))
        assert read_market_capture(conn, "volume", expected_sha256=raw.digest()).result_bytes() == (
            raw.result_bytes()
        )
    context = stored_market_context(path, asof=CLOCK.date(), known_at=CLOCK)
    assert context["coverage"] == ("partial" if invalid else "received")
    view = context["bars"][0]
    assert view["receipt_id"] == "volume"
    assert view["capture_sha256"] == raw.digest()
    assert view["status"] == "received"
    assert [view["last_bar"][k] for k in ("o", "h", "l", "c")] == [10, 12, 9, 11]
    if invalid:
        assert view["last_bar"]["v"] is None
        assert view["volume_status"] == "invalid"
    else:
        assert view["last_bar"]["v"] == volume
        assert "volume_status" not in view


@pytest.mark.parametrize("volumes", [(1, True), (True, 1), (0, False), (False, 0)])
def test_equal_clock_does_not_equate_boolean_and_numeric_volume(tmp_path, volumes):
    path = tmp_path / "economic.sqlite"
    with closing(initialize_database(path)) as conn:
        for rid, v in zip(("a", "b"), volumes, strict=True):
            retain_market_capture(conn, _capture_with_volume(rid, v))
    bar = stored_market_context(path, asof=CLOCK.date(), known_at=CLOCK)["bars"][0]
    assert bar["status"] == "ambiguous_same_time"
    assert set(bar["conflicting_receipt_ids"]) == {"a", "b"}
    assert "last_bar" not in bar


def test_future_and_inconsistent_bars_do_not_become_prices(tmp_path):
    from baibai_engine.data_collection.providers.tradingview.market_models import MarketToolCapture

    path = tmp_path / "economic.sqlite"
    future = capture(receipt_id="future-bar", bars=True).model_dump(by_alias=True)
    future["result"]["structuredContent"]["bars"][-1]["t"] = int(
        (CLOCK + timedelta(minutes=5)).timestamp()
    )
    with closing(initialize_database(path)) as c:
        retain_market_capture(c, MarketToolCapture.model_validate(future))
    context = stored_market_context(
        path, asof=date(2026, 1, 5), known_at=CLOCK + timedelta(hours=1)
    )
    assert context["bars"][0]["status"] == "future_bar_timestamp"
    assert context["bars"][0]["last_bar"] is None
    assert context["coverage"] == "partial"
