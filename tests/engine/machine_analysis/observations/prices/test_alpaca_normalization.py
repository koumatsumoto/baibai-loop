"""Normalize actual wire-contract shapes with mock captures, never a provider transport."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

import pytest

from baibai_engine.data_collection.providers.alpaca import (
    AlpacaCaptureIncomplete,
    BarsCapture,
    BarsPage,
    BarsResponse,
    DailyBarsQuery,
    capture_daily_bars,
)
from baibai_engine.machine_analysis.observations.prices.alpaca_normalization import (
    normalize_alpaca_daily_bars,
)

_QUERY = DailyBarsQuery(("META", "BRK.B"), date(2025, 1, 15), date(2025, 7, 13), "-", "raw")
_RECEIVED = datetime.fromisoformat("2026-11-23T10:15:00Z")
_LATER = datetime.fromisoformat("2026-11-23T10:16:00Z")
_WINTER = "2025-01-15T05:00:00Z"
_SUMMER = "2025-07-11T04:00:00Z"


def _row(timestamp: str = _WINTER, **changes: object) -> dict[str, object]:
    return {"t": timestamp, "o": 100, "h": 104, "l": 98, "c": 101, "v": 732, **changes}


def _page(
    groups: dict[str, list[dict[str, object]]] | None = None,
    cursor: str | None = None,
    *,
    received: datetime = _RECEIVED,
) -> BarsResponse:
    body = json.dumps(
        {"bars": {} if groups is None else groups, "next_page_token": cursor}
    ).encode()
    return BarsResponse(200, received, body)


def _capture(
    *responses: BarsResponse, query: DailyBarsQuery = _QUERY, request_id: str = "fixture-a"
) -> BarsCapture:
    stream = iter(responses)
    return capture_daily_bars(query, request_id=request_id, transport=lambda _: next(stream))


def test_exact_native_query_and_row_evidence_survive_normalization() -> None:
    capture = _capture(
        _page({"META": [_row(v=0)]}, "opaque+cursor"),
        _page({"BRK.B": [_row(_SUMMER)]}, received=_LATER),
    )
    winter, summer = normalize_alpaca_daily_bars(capture)
    assert normalize_alpaca_daily_bars(capture) == (winter, summer)
    assert (winter.returned_symbol, winter.requested_symbol, winter.alias_status) == (
        "META",
        "META",
        "literal_requested_label",
    )
    assert (summer.returned_symbol, summer.requested_symbol, summer.alias_status) == (
        "BRK.B",
        "BRK.B",
        "literal_requested_label",
    )
    assert (winter.trade_date, summer.trade_date) == (date(2025, 1, 15), date(2025, 7, 11))
    assert winter.bar_started_at == datetime.fromisoformat(_WINTER)
    assert summer.bar_started_at == datetime.fromisoformat(_SUMMER)
    assert (winter.feed, winter.price_unit, winter.price_basis) == (
        "iex",
        "USD_per_share",
        "vendor_daily_aggregate",
    )
    assert winter.adjustment == "raw"
    assert winter.symbol_asof == "-"
    assert (winter.open, winter.high, winter.low, winter.close, winter.volume) == (
        Decimal(100),
        Decimal(104),
        Decimal(98),
        Decimal(101),
        Decimal(0),
    )
    for number, observation, pointer in (
        (0, winter, "/bars/META/0"),
        (1, summer, "/bars/BRK.B/0"),
    ):
        (source,) = observation.sources
        assert source.receipt_id == capture.pages[number].receipt_id
        assert source.body_sha256 == capture.pages[number].body_sha256
        assert source.received_at == capture.pages[number].response.received_at
        assert source.json_pointer == pointer


def test_dated_provider_alias_is_unresolved_even_if_label_matches_a_request() -> None:
    dated = replace(_QUERY, symbol_asof=date(2025, 7, 13))
    capture = _capture(_page({"FB": [_row()], "META": [_row()]}), query=dated)
    observations = normalize_alpaca_daily_bars(capture)
    assert {row.returned_symbol for row in observations} == {"META", "FB"}
    for row in observations:
        assert row.symbol_asof == date(2025, 7, 13)
        assert row.requested_symbol is None
        assert row.alias_status == "unresolved_provider_mapping"


def test_split_and_raw_decimal_amounts_are_not_rounded_or_equated() -> None:
    fraction = "101.00000000000000000012345"
    share_fraction = "1234.1234567890123456789"
    response = _page({"META": [_row(c="DECIMAL_PRICE", v="DECIMAL_VOLUME")]})
    wire_bytes = response.body.replace(b'"DECIMAL_PRICE"', fraction.encode()).replace(
        b'"DECIMAL_VOLUME"',
        share_fraction.encode(),
    )
    split_query = replace(_QUERY, adjustment="split")
    (split,) = normalize_alpaca_daily_bars(
        _capture(replace(response, body=wire_bytes), query=split_query)
    )
    assert split.adjustment == "split"
    assert str(split.close) == fraction
    assert str(split.volume) == share_fraction
    (raw,) = normalize_alpaca_daily_bars(_capture(replace(response, body=wire_bytes)))
    assert raw.adjustment == "raw"
    assert raw.sources[0].receipt_id != split.sources[0].receipt_id


def test_summer_ny_wall_clock_offset_normalizes_and_creates_no_other_dates() -> None:
    stamp = "2025-07-11T00:00:00.000000000-04:00"
    # Friday morning receipt is not evidence of Friday official close or of an opening calendar.
    before_close = datetime.fromisoformat("2025-07-11T14:00:00Z")
    (one,) = normalize_alpaca_daily_bars(
        _capture(_page({"META": [_row(stamp)]}, received=before_close))
    )
    assert one.trade_date == date(2025, 7, 11)
    assert one.bar_started_at == datetime.fromisoformat(_SUMMER)
    assert one.sources[0].received_at == before_close
    assert one.sources[0].native_timestamp == stamp


@pytest.mark.parametrize(
    "timestamp",
    [
        "2025-01-15T04:00:00Z",
        "2025-07-11T05:00:00Z",
        "2025-07-11T16:00:00Z",
        "2025-07-11T04:00:00.000000001Z",
        "2025-07-11T04:00:00-00:00",
        "2025-07-11T00:00:00-03:60",
        "2025-07-11T00:00:00",
        "2025-07-11",
    ],
)
def test_approximate_or_intraday_timestamp_is_not_a_daily_observation(timestamp: str) -> None:
    with pytest.raises(ValueError, match="Alpaca daily bar timestamp"):
        normalize_alpaca_daily_bars(_capture(_page({"META": [_row(timestamp)]})))


def test_daily_bar_timestamp_must_fit_fixed_query_and_not_be_after_receipt() -> None:
    with pytest.raises(ValueError, match="outside the fixed Alpaca interval"):
        normalize_alpaca_daily_bars(_capture(_page({"META": [_row("2025-07-14T04:00:00Z")]})))
    early = datetime.fromisoformat("2025-07-11T03:59:59Z")
    with pytest.raises(ValueError, match="Alpaca daily bar starts after its receipt"):
        normalize_alpaca_daily_bars(_capture(_page({"META": [_row(_SUMMER)]}, received=early)))


@pytest.mark.parametrize(
    "changes",
    [{"v": False}, {"v": True}, {"v": -1}, {"o": True}, {"c": "101"}, {"l": None}, {"h": 90}],
)
def test_malformed_ohlcv_is_neither_zero_nor_absent(changes: dict[str, object]) -> None:
    capture = _capture(_page({"META": [_row(**changes)]}))
    assert capture.complete  # Wire success does not normalize an invalid field.
    with pytest.raises(ValueError, match="invalid Alpaca OHLCV"):
        normalize_alpaca_daily_bars(capture)


def test_complete_empty_is_not_partial_or_malformed() -> None:
    empty = _capture(_page())
    assert normalize_alpaca_daily_bars(empty) == ()

    failed = _capture(_page({"META": [_row()]}, "next"), BarsResponse(403, _LATER, b"blocked"))
    with pytest.raises(AlpacaCaptureIncomplete, match="http_status"):
        normalize_alpaca_daily_bars(failed)

    malformed = _capture(
        BarsResponse(200, _RECEIVED, b'{"bars":{"META":[{"c":NaN}]},"next_page_token":null}')
    )
    with pytest.raises(AlpacaCaptureIncomplete, match="invalid_envelope"):
        normalize_alpaca_daily_bars(malformed)


def test_wrong_scope_reordered_or_truncated_native_receipt_chain_is_rejected() -> None:
    first = _page({"META": [_row()]}, "next")
    other = _page({"BRK.B": [_row(_SUMMER)]}, received=_LATER)
    capture = _capture(first, other)
    foreign_request = _capture(first, other, request_id="fixture-b")
    foreign_basis = _capture(first, other, query=replace(_QUERY, adjustment="split"))
    for wrong in (foreign_request.pages[1], foreign_basis.pages[1]):
        with pytest.raises(
            ValueError, match="Alpaca page receipt does not match the fixed capture"
        ):
            normalize_alpaca_daily_bars(replace(capture, pages=(capture.pages[0], wrong)))
    with pytest.raises(ValueError, match="Alpaca page receipt does not match the fixed capture"):
        normalize_alpaca_daily_bars(replace(capture, pages=tuple(reversed(capture.pages))))
    with pytest.raises(ValueError, match="Alpaca fixed capture has no terminal receipt"):
        normalize_alpaca_daily_bars(replace(capture, pages=(capture.pages[0],)))


def test_replayed_cycle_is_rejected_even_after_a_terminal_response_is_appended() -> None:
    first = _page({"META": [_row()]}, "repeat")
    repeated = _page({"BRK.B": [_row(_SUMMER)]}, "repeat", received=_LATER)
    cyclic = _capture(first, repeated)
    assert cyclic.failure == "pagination_cycle"
    terminal = BarsPage(
        cyclic.request_id, 3, _QUERY.url(page_token="repeat"), _page(received=_LATER), ()
    )
    edited = replace(cyclic, failure=None, pages=(*cyclic.pages, terminal))
    with pytest.raises(ValueError, match="pagination_cycle"):
        normalize_alpaca_daily_bars(edited)


def test_replay_cannot_treat_a_custom_bound_failure_as_a_search_with_more_pages() -> None:
    first = _page({"META": [_row()]}, "next")
    bounded = capture_daily_bars(
        _QUERY,
        request_id="bound-fixture",
        transport=lambda _: first,
        max_pages=1,
    )
    assert bounded.failure == "page_limit"
    terminal = BarsPage(
        bounded.request_id, 2, _QUERY.url(page_token="next"), _page(received=_LATER), ()
    )
    edited = replace(bounded, failure=None, pages=(*bounded.pages, terminal))
    with pytest.raises(ValueError, match="page_limit"):
        normalize_alpaca_daily_bars(edited)


def test_replay_cannot_treat_reversed_receipts_as_a_valid_fixed_chain() -> None:
    captured = _capture(_page({"META": [_row()]}, "next", received=_LATER), _page())
    with pytest.raises(ValueError, match="receipt_time_order"):
        normalize_alpaca_daily_bars(replace(captured, failure=None))


def test_forged_empty_bars_group_cannot_bypass_the_native_envelope_contract() -> None:
    dated = replace(_QUERY, symbol_asof=date(2025, 7, 13))
    received = _page({"bad\nlabel": []})
    captured = _capture(received, query=dated)
    assert captured.failure == "invalid_envelope"
    forged_page = BarsPage(captured.request_id, 1, dated.url(), received, ("bad\nlabel",))
    with pytest.raises(ValueError, match="invalid_envelope"):
        normalize_alpaca_daily_bars(replace(captured, failure=None, pages=(forged_page,)))


def test_equal_rows_keep_distinct_receipts_but_conflicting_rows_are_never_chosen() -> None:
    first = _page({"META": [_row()]}, "next")
    repeated = _page({"META": [_row()]}, received=_LATER)
    capture = _capture(first, repeated)
    (row,) = normalize_alpaca_daily_bars(capture)
    assert [ref.receipt_id for ref in row.sources] == [page.receipt_id for page in capture.pages]
    assert [ref.received_at for ref in row.sources] == [_RECEIVED, _LATER]

    for pair in ((101, 102), (102, 101)):
        with pytest.raises(ValueError, match="conflicting Alpaca daily observations"):
            normalize_alpaca_daily_bars(
                _capture(
                    _page({"META": [_row(c=pair[0])]}, "next"),
                    _page({"META": [_row(c=pair[1])]}, received=_LATER),
                )
            )
