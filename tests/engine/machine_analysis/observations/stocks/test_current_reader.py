from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from tests.helpers.screening_sqlite import make_master_records

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.stocks.contracts import MasterSnapshot
from baibai_engine.machine_analysis.observations.stocks.current_reader import read_current_jp_master
from baibai_engine.machine_analysis.observations.stocks.jquants_listing import normalize_jp_listings
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    JapanListingAttributes,
    ListingMasterRow,
    ListingSlice,
    refresh_listing_slice,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    CurrentListingMarket,
    decode_current_listing_master,
    encode_current_listing_master,
)
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import (
    normalize_master_record,
)
from baibai_engine.primitives.time import JST

_THU = date(2026, 11, 26)
_FRI = date(2026, 11, 27)
_SAT = date(2026, 11, 28)
_SUN = date(2026, 11, 29)
_MON = date(2026, 11, 30)
_RECEIVED = datetime(2026, 11, 27, 8, 30, tzinfo=UTC)
_AFTER = datetime(2026, 11, 30, 8, 30, tzinfo=UTC)
_DOMESTIC = JapanListingAttributes("011", "プライム", "情報･通信業")
_ETF = JapanListingAttributes("014", "ETF・ETN", "その他")


@pytest.fixture
def calendar():
    with closing(sqlite3.connect(":memory:")) as session:
        # Only the calendar exists. An apparent success cannot read old master tables.
        session.execute(
            "CREATE TABLE jquants_market_calendar (day TEXT PRIMARY KEY, holiday_division TEXT)"
        )
        session.executemany(
            "INSERT INTO jquants_market_calendar VALUES (?, ?)",
            (
                (_THU.isoformat(), "1"),
                (_FRI.isoformat(), "2"),  # Half session is still an exact open session.
                (_SAT.isoformat(), "0"),
                (_SUN.isoformat(), "3"),  # Futures-only is closed for cash equities.
                (_MON.isoformat(), "1"),
            ),
        )
        yield session


def _current(session=_FRI, received_at=_RECEIVED):
    return CurrentListingMarket(
        ListingSlice(
            market="JP",
            session=session,
            rows=(
                ListingMasterRow(("XTKS", "55550"), None, None, "scheduled"),
                ListingMasterRow(
                    ("XTKS", "56785"),
                    "別の株式クラス",
                    "preferred_stock",
                    "listed",
                    japan=_DOMESTIC,
                ),
                ListingMasterRow(("XTKS", "90000"), "比較ETF", "etf", "listed", japan=_ETF),
                ListingMasterRow(
                    ("XTKS", "12A40"), "本銘柄", "common_stock", "listed", japan=_DOMESTIC
                ),
                ListingMasterRow(
                    ("XTKS", "44440"), "旧銘柄", "common_stock", "unverified", japan=_DOMESTIC
                ),
                ListingMasterRow(("XNGO", "12A40"), "同コード別市場", None, "listed"),
            ),
        ),
        source="jquants",
        received_at=received_at,
        received_count=4,
    )


def test_exact_open_projection_filters_native_primary_code_and_listing_states(calendar):
    snapshot = read_current_jp_master(
        _current(), calendar=calendar, as_of=_FRI, evaluation_time=_AFTER
    )
    assert snapshot is not None
    assert snapshot.as_of == _FRI
    assert list(snapshot.securities) == ["12A4", "9000"]
    primary = snapshot.securities["12A4"]
    assert (primary.name, primary.market_segment, primary.sector_33, primary.is_common_stock) == (
        "本銘柄",
        "プライム",
        "情報・通信業",
        True,
    )
    assert not snapshot.securities["9000"].is_common_stock


@pytest.mark.parametrize("tickers", [(), ("5678", "4444", "5555", "9999")])
def test_eligible_date_is_chosen_before_an_empty_ticker_selection(calendar, tickers):
    snapshot = read_current_jp_master(
        _current(), calendar=calendar, as_of=_FRI, evaluation_time=_AFTER, tickers=tickers
    )
    assert snapshot == MasterSnapshot(_FRI, {})


def test_ticker_selection_normalizes_requested_tickers_but_not_different_issue_codes(calendar):
    snapshot = read_current_jp_master(
        _current(),
        calendar=calendar,
        as_of=_FRI,
        evaluation_time=_AFTER,
        tickers=(" 12a4 ", "12A4", "5678"),
    )
    assert snapshot is not None
    assert list(snapshot.securities) == ["12A4"]


@pytest.mark.parametrize("closed_day", [_SAT, _SUN])
def test_fully_confirmed_cash_closure_uses_same_pinned_previous_open_session(calendar, closed_day):
    snapshot = read_current_jp_master(
        _current(), calendar=calendar, as_of=closed_day, evaluation_time=_AFTER
    )
    assert snapshot is not None
    assert snapshot.as_of == _FRI


@pytest.mark.parametrize("damage", ["closure_gap", "unknown_closure", "unknown_open", "no_basis"])
def test_no_verified_cash_calendar_basis_means_unavailable_not_empty(calendar, damage):
    day = _SUN
    if damage == "closure_gap":
        calendar.execute("DELETE FROM jquants_market_calendar WHERE day=?", (_SAT.isoformat(),))
    elif damage == "unknown_closure":
        calendar.execute(
            "UPDATE jquants_market_calendar SET holiday_division='unknown' WHERE day=?",
            (_SAT.isoformat(),),
        )
    elif damage == "unknown_open":
        day = _FRI
        calendar.execute("DELETE FROM jquants_market_calendar WHERE day=?", (_FRI.isoformat(),))
    else:
        calendar.execute("DELETE FROM jquants_market_calendar WHERE day<=?", (_FRI.isoformat(),))
    assert (
        read_current_jp_master(_current(), calendar=calendar, as_of=day, evaluation_time=_AFTER)
        is None
    )


@pytest.mark.parametrize(
    ("session", "as_of"),
    [
        (_THU, _FRI),  # Today's open session must not read stale data.
        (_FRI, _MON),  # A weekend basis is no longer valid on the next open day.
        (_THU, _SUN),  # Even on a closed date, only the exact previous session.
        (_MON, _SUN),  # Later section never supplies an earlier confirmed basis.
        (_FRI, _THU),  # No historical replay from the current file.
    ],
)
def test_wrong_session_never_projects_another_day(calendar, session, as_of):
    assert (
        read_current_jp_master(
            _current(session), calendar=calendar, as_of=as_of, evaluation_time=_AFTER
        )
        is None
    )


def test_missing_jp_section_is_not_an_empty_valid_market(calendar):
    current = _current()
    us_only = SimpleNamespace(
        slice=replace(current.slice, market="US"), received_at=current.received_at
    )
    for entry in (None, us_only):
        assert (
            read_current_jp_master(entry, calendar=calendar, as_of=_FRI, evaluation_time=_AFTER)
            is None
        )


def test_receipt_view_is_pinned_even_when_a_newer_same_day_correction_exists(calendar):
    initial = _current()
    later = replace(
        initial,
        received_at=_RECEIVED + timedelta(seconds=1),
        slice=replace(
            initial.slice,
            rows=tuple(
                replace(row, security_name="訂正後") if row.key == ("XTKS", "12A40") else row
                for row in initial.slice.rows
            ),
        ),
    )
    kwargs = {"calendar": calendar, "as_of": _FRI, "tickers": ("12A4",)}
    pinned = read_current_jp_master(initial, evaluation_time=_RECEIVED.astimezone(JST), **kwargs)
    assert pinned is not None
    assert pinned.securities["12A4"].name == "本銘柄"
    assert read_current_jp_master(later, evaluation_time=_RECEIVED, **kwargs) is None
    current = read_current_jp_master(later, evaluation_time=_AFTER, **kwargs)
    assert current is not None
    assert current.securities["12A4"].name == "訂正後"
    assert pinned.securities["12A4"].name == "本銘柄"


def test_jst_effective_day_time_boundary_allows_advance_source_receipt_not_advance_use(calendar):
    available = _current(received_at=datetime(2026, 11, 26, 8, 30, tzinfo=UTC))
    jst_midnight = datetime(2026, 11, 26, 15, tzinfo=UTC)
    assert (
        read_current_jp_master(
            available,
            calendar=calendar,
            as_of=_FRI,
            evaluation_time=jst_midnight - timedelta(microseconds=1),
        )
        is None
    )
    assert (
        read_current_jp_master(
            available, calendar=calendar, as_of=_FRI, evaluation_time=jst_midnight
        )
        is not None
    )


def test_timezone_naive_cutoff_is_a_caller_error(calendar):
    with pytest.raises(ValueError, match="aware evaluation time"):
        read_current_jp_master(
            _current(),
            calendar=calendar,
            as_of=_FRI,
            evaluation_time=_RECEIVED.replace(tzinfo=None),
        )


@pytest.mark.parametrize("code", ["13A4", "13A4 ", "13a40", "13A4٠"])
@pytest.mark.parametrize("tickers", [None, ("13A4",)])
def test_invalid_primary_code_cannot_become_a_verified_absence(calendar, code, tickers):
    good = _current()
    bad = ListingMasterRow(
        ("XTKS", code), "誤ったコード", "common_stock", "listed", japan=_DOMESTIC
    )
    invalid = replace(good, slice=replace(good.slice, rows=(*good.slice.rows, bad)))
    with pytest.raises(JQuantsProviderError, match="exact five-character Code"):
        read_current_jp_master(
            invalid, calendar=calendar, as_of=_FRI, evaluation_time=_AFTER, tickers=tickers
        )


@pytest.mark.parametrize(
    ("key", "attrs", "name"),
    [
        (("XTKS", "22220"), None, "分類不明"),
        (("XTKS", "22220"), replace(_DOMESTIC, product_category=" "), "商品区分不明"),
        (("XTKS", "22220"), _DOMESTIC, None),
    ],
)
def test_malformed_active_jp_data_never_silently_supplies_classification(
    calendar, key, attrs, name
):
    good = _current()
    bad_row = ListingMasterRow(key, name, "common_stock", "listed", japan=attrs)
    invalid = replace(good, slice=replace(good.slice, rows=(bad_row, *good.slice.rows)))
    with pytest.raises(JQuantsProviderError, match="current JP master"):
        read_current_jp_master(invalid, calendar=calendar, as_of=_FRI, evaluation_time=_AFTER)


def test_existing_jp_master_projection_matches_the_shared_serialized_section(calendar):
    records = make_master_records(_FRI)
    records[0].update(Code="12a40", S33Nm="情報･通信業")
    records.extend(
        (
            dict(records[0], Code="12A45", CoName="優先"),
            dict(records[0], Code="90000", CoName="比較ETF", ProdCat="014"),
            dict(records[0], Code="90010", CoName="比較REIT", ProdCat="013"),
        )
    )
    capture = normalize_jp_listings(
        [
            JQuantsPage(
                "master",
                {"date": _FRI.isoformat()},
                _RECEIVED,
                json.dumps({"data": records}).encode(),
            )
        ],
        session=_FRI,
    )
    new = refresh_listing_slice(
        None, market="JP", session=_FRI, observations=capture.observations, complete=True
    ).slice
    entry = CurrentListingMarket(new, "jquants", capture.received_at, len(capture.observations))
    pinned = decode_current_listing_master(encode_current_listing_master({"JP": entry}))
    actual = read_current_jp_master(
        pinned["JP"], calendar=calendar, as_of=_FRI, evaluation_time=_RECEIVED
    )
    expected = {
        security.ticker: security
        for record in records
        if (security := normalize_master_record(record)) is not None
    }
    assert len(expected) == 2502
    assert actual == MasterSnapshot(_FRI, expected)
