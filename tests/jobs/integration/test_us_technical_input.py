"""Local acceptance of split SIP technical input; no external reads or stores."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

import pytest
from tests.helpers.us_research_admission import make_current_us

from baibai_engine.data_collection.providers.alpaca import (
    BarsResponse,
    DailyBarsQuery,
    capture_daily_bars,
)
from baibai_engine.data_collection.providers.alpaca_calendar import (
    AlpacaCalendarResponse,
    read_paper_us_calendar,
)
from baibai_engine.machine_analysis.models.technical_analysis.generation import (
    generate_technical_analysis,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    active_us_listing_sha256,
    encode_current_listing_master,
)
from baibai_jobs.integration.us_technical_input import admit_us_technical_input

_NY = ZoneInfo("America/New_York")
_FRI = date(2026, 11, 27)
_SAT = _FRI + timedelta(days=1)
_MON = _FRI + timedelta(days=3)
_CUTOFF = datetime(2026, 11, 30, 14, 55, tzinfo=UTC)
_DAYS = tuple(
    sorted(
        day
        for i in range(380)
        if (day := _FRI - timedelta(days=i)).weekday() < 5 and day != date(2026, 11, 26)
    )[-253:]
)
_EARLIEST = _DAYS[0]
_TICKER = "BRK.B"


def _calendars(*, start=_EARLIEST, end=_MON, cutoff=_CUTOFF, open_days=_DAYS):
    readings = []
    opened = {*open_days, _MON}
    while start <= end:
        through = min(start + timedelta(days=30), end)
        body = json.dumps(
            [
                {
                    "date": day.isoformat(),
                    "open": "09:30",
                    "close": "13:00" if day == _FRI else "16:00",
                }
                for day in sorted(opened)
                if start <= day <= through
            ]
        ).encode()
        readings.append(
            read_paper_us_calendar(
                start,
                through,
                get_authorized=lambda url, body=body: AlpacaCalendarResponse(
                    url, 200, cutoff - timedelta(seconds=5), body, True
                ),
            )
        )
        start = through + timedelta(days=1)
    return tuple(readings)


def _listing(
    *,
    identity_since=_EARLIEST,
    effective_session=None,
    day=_MON,
    second=False,
    instrument_id="id-B",
):
    master = make_current_us(
        venue="XNYS",
        ticker=_TICKER,
        day=day,
        identity_since=identity_since,
        instrument_id=instrument_id,
    )
    if effective_session is not None:
        master = replace(
            master,
            slice=replace(
                master.slice,
                rows=(replace(master.slice.rows[0], effective_session=effective_session),),
            ),
        )
    if second:
        extra = make_current_us(
            venue="XNAS",
            ticker="GOOGL",
            day=day,
            identity_since=identity_since,
            instrument_id="id-A",
        )
        rows = (*master.slice.rows, *extra.slice.rows)
        assert master.rakuten is not None
        assert extra.rakuten is not None
        ids = {row.key: row.instrument_id for row in rows}
        master = replace(
            master,
            slice=replace(master.slice, rows=rows),
            received_count=2,
            rakuten=replace(
                master.rakuten,
                catalog_count=2,
                active_listing_sha256=active_us_listing_sha256(ids),
                rows=(*master.rakuten.rows, *extra.rakuten.rows),
            ),
        )
    return encode_current_listing_master({"US": master})


def _prices(day):
    return 120 + (_DAYS.index(day) % 17) / 10


def _capture(
    *,
    days=_DAYS,
    symbols=(_TICKER,),
    absent=(),
    feed="sip",
    adjustment="split",
    asof="-",
    clock=_CUTOFF,
    omit_terminal=False,
):
    query = DailyBarsQuery(symbols, days[0], days[-1], asof, adjustment, feed)
    # The same owner checks a real two-page continuation with DST-correct timestamps.
    middle = len(days) // 2
    pages = iter((days[:middle], days[middle:]))

    def fetch(_url):
        span = next(pages)
        body = {
            "bars": {
                symbol: [
                    {
                        "t": datetime.combine(day, time.min, tzinfo=_NY)
                        .astimezone(UTC)
                        .isoformat(),
                        "o": 120,
                        "h": 122,
                        "l": 119,
                        "c": _prices(day),
                        "v": 203,
                    }
                    for day in span
                    if day not in absent
                ]
                for symbol in symbols
            },
            "currency": "USD",
        }
        if span == days[:middle]:
            body["next_page_token"] = "page-two"
        elif not omit_terminal:
            body["next_page_token"] = None
        return BarsResponse(200, clock, json.dumps(body).encode())

    return capture_daily_bars(query, request_id="synthetic-admitted-split-panel", transport=fetch)


def _admit(
    captures=None,
    *,
    listing=None,
    sha=None,
    calendars=None,
    cutoff=_CUTOFF,
    last_admitted_session=None,
):
    listing = _listing() if listing is None else listing
    return admit_us_technical_input(
        listing_bytes=listing,
        captured_listing_sha256=sha256(listing).hexdigest() if sha is None else sha,
        calendars=_calendars(cutoff=cutoff) if calendars is None else calendars,
        captures=(_capture(clock=cutoff),) if captures is None else captures,
        cutoff=cutoff,
        last_admitted_session=last_admitted_session,
    )


def test_uses_market_matched_full_split_panel_and_same_calculations_as_jp():
    result = _admit((_capture(symbols=(_TICKER, "GOOGL")),), listing=_listing(second=True))
    assert (result.listing_session, result.price_session) == (_MON, _FRI)
    brk, googl = result.models
    assert (brk.subject, googl.subject) == ("id-B", "id-A")
    assert brk.payload["ticker"] == "BRK.B"
    assert googl.payload["ticker"] == "GOOGL"
    assert brk.payload["price_unit"] == "USD_per_share"
    assert brk.payload["price_source"] == "alpaca_sip_daily_split"
    assert brk.payload["rsi_seed_scope"] == "retained_ny_sessions"
    assert brk.payload["earlier_current_key_history"] == "outside_window"
    jp = generate_technical_analysis(
        [PriceBar(day, _prices(day), None) for day in _DAYS],
        ticker="7203",
        as_of=_FRI,
        trading_dates=_DAYS,
    )
    keys = ("moving_averages", "rsi_14bar", "price_momentum", "bollinger_20bar", "bar_count")
    assert all(brk.payload[key] == jp[key] for key in keys)
    assert brk.payload["moving_averages"]["sma_200bar"]["status"] == "available"
    assert brk.payload["price_momentum"]["start_date"] == _DAYS[0].isoformat()
    assert brk.payload["price_momentum"]["end_date"] == _DAYS[-22].isoformat()
    assert brk.payload["rsi_14bar"]["seed_end_date"] == _DAYS[14].isoformat()
    path = brk.payload["price_path"]
    assert path["row_state_basis"] == "alpaca_sip_split_daily_ohlcv"
    assert path["no_trade_reason"] is None
    assert path["adjustment_as_of"] is None
    assert path["read_semantics"] == "fixed_vendor_split_at_receipt_not_original_vintage"
    assert path["expected_price_date"] == _FRI.isoformat()
    assert path["observations"]["R20"]["value"] == pytest.approx(
        _prices(_FRI) / _prices(_DAYS[-21]) - 1
    )
    assert path["observations"]["R1"]["comparison_start_date"] == _DAYS[-2].isoformat()


def test_recent_initial_listing_reports_short_history_and_does_not_adopt_older_ticker_price():
    listing = _listing(identity_since=_FRI)
    assert _admit(listing=listing) is None
    model = _admit((_capture(days=(_FRI,)),), listing=listing).models[0]
    assert model.payload["bar_count"] == 1
    assert model.payload["history_scope_start"] == _FRI.isoformat()
    assert model.payload["earlier_current_key_history"] == "unverified"
    assert model.payload["moving_averages"]["sma_20bar"]["status"] == "insufficient_history"
    assert model.payload["price_momentum"]["status"] == "missing_endpoint"


def test_verified_move_floor_never_relabels_before_effective_session():
    listing = _listing(effective_session=_DAYS[-23])
    assert _admit(listing=listing) is None
    assert (
        _admit((_capture(days=_DAYS[-23:]),), listing=listing).models[0].payload["bar_count"] == 23
    )


def test_whole_chain_can_explain_missing_history_but_not_invent_current_or_momentum():
    absent = (_DAYS[0], _DAYS[-8], _DAYS[-21])
    model = _admit((_capture(absent=absent),)).models[0]
    path = model.payload["price_path"]
    assert path["observations"]["R20"]["unavailable_reason"] == "missing_start_price"
    assert path["observations"]["R1"]["value"] is not None
    assert model.payload["price_momentum"]["status"] == "missing_endpoint"
    assert (
        _DAYS[-8].isoformat() in path["observations"]["R20"]["coverage"]["unpriced_dates"]["absent"]
    )
    assert _admit((_capture(absent=(_FRI,)),)) is None


@pytest.mark.parametrize(
    "options",
    [
        {"feed": "iex"},
        {"adjustment": "raw"},
        {"omit_terminal": True},
        {"asof": _FRI},
        {"days": _DAYS[1:]},
        {"days": _DAYS[:-1]},
        {"clock": _CUTOFF + timedelta(seconds=1)},
        {"clock": _CUTOFF - timedelta(seconds=6)},
    ],
)
def test_inapplicable_or_incomplete_capture_never_creates_a_generation(options):
    assert _admit((_capture(**options),)) is None


def test_scope_mismatch_duplicate_or_unattempted_target_never_appear_complete():
    assert _admit(sha="0" * 64) is None
    assert _admit((_capture(), _capture())) is None
    assert _admit(listing=_listing(second=True)) is None
    assert _admit((_capture(symbols=("BRK.A",)),)) is None


def test_same_label_and_date_with_a_different_valid_pin_does_not_attach_old_prices():
    acquired = _listing()
    other = _listing(instrument_id="different-valid-current-issue")
    assert _admit(listing=other).models[0].subject == "different-valid-current-issue"
    assert _admit(listing=other, sha=sha256(acquired).hexdigest()) is None


def test_exact_prior_short_session_can_be_admitted_once_on_weekend_but_not_fake_closed_rows():
    cutoff = datetime.combine(_SAT, time(14), tzinfo=UTC)
    listing = _listing(day=_FRI)
    result = _admit(listing=listing, cutoff=cutoff)
    assert (result.listing_session, result.price_session) == (_FRI, _FRI)
    assert _admit(listing=listing, cutoff=cutoff, last_admitted_session=_FRI) is None
    assert _admit(listing=listing) is None  # Monday is open; not a current Monday broker scope.
    assert _admit(listing=listing, cutoff=cutoff, calendars=_calendars(end=_FRI)) is None


def test_calendar_252_or_missing_civil_window_is_unavailable_not_shortened():
    assert _admit(calendars=_calendars(start=_DAYS[1])) is None
    calendars = _calendars()
    assert _admit(calendars=(*calendars[:4], *calendars[5:])) is None
