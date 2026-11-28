from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from tests.jobs.integration.test_listing_master_current import _refresh, _Store

from baibai_engine.data_collection.providers.alpaca import MAX_SYMBOLS
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    ObservedListing,
    VerifiedListingEvent,
)
from baibai_engine.machine_analysis.observations.stocks.us_universe import (
    parse_rakuten_ordinary_snapshot,
    rakuten_listing_key,
)
from baibai_jobs.integration.listing_master_current import (
    RakutenListingBinding,
    read_current_listing_master,
    refresh_current_us_rakuten_scope,
)
from baibai_jobs.integration.us_daily_plan import plan_current_us_daily_prices

_FRI = date(2026, 11, 27)
_MON = date(2026, 11, 30)
_FRI_CLOCK = datetime(2026, 11, 27, 14, tzinfo=UTC)
_MON_CLOCK = datetime(2026, 11, 30, 14, tzinfo=UTC)
_CUTOFF = _MON_CLOCK + timedelta(minutes=20)
_ESTABLISHED = ("BRK.B", "GOOG", *(f"US{i:02}" for i in range(2 * MAX_SYMBOLS)))


def _listing(symbol, venue="XNAS", *, kind="not_flagged_fund"):
    return ObservedListing((venue, symbol), f"Synthetic {symbol}", kind)


def _store():
    store = _Store()
    before = (
        _listing("BRK.B", "XNYS"),
        _listing("BRK.A", "XNYS"),
        _listing("WMT", "XNYS"),
        *(_listing(symbol) for symbol in _ESTABLISHED[1:]),
        _listing("REUSE"),
        _listing("SELL"),
        _listing("STOP"),
        _listing("BOTH"),
        _listing("FUND", kind="fund"),
        _listing("BAD P", "XNYS"),
    )
    assert _refresh(store, "US", before, day=_FRI, received=_FRI_CLOCK) == "updated"
    after = (
        *(row for row in before if row.key not in (("XNYS", "WMT"), ("XNAS", "REUSE"))),
        _listing("IPO"),
        _listing("WMT"),
        replace(_listing("REUSE"), security_name="Different issue after symbol reuse"),
        _listing("BOTH", "XNYS"),
    )
    events = (
        VerifiedListingEvent("moved", ("XNAS", "WMT"), _MON, "verified move", ("XNYS", "WMT")),
        VerifiedListingEvent("reused", ("XNAS", "REUSE"), _MON, "verified different issue"),
    )
    assert _refresh(store, "US", after, day=_MON, received=_MON_CLOCK, events=events) == "updated"
    return store


def _attach(store, *, catalog=None, venue_counts=None, restrictions=None, clock=_MON_CLOCK):
    catalog = catalog or (
        "ticker,exchange\nBRK B,NYSE\n"
        + "".join(f"{symbol},NASDAQ\n" for symbol in _ESTABLISHED[1:])
        + "IPO,NASDAQ\nREUSE,NASDAQ\nWMT,NASDAQ\nSTOP,NASDAQ\nFUND,NASDAQ\n"
        "BAD P,NYSE\nMISSING,NASDAQ\nBOTH,NASDAQ\nBRK A,NYSE American\n"
    )
    if venue_counts is None:
        venue_counts = {
            "NASDAQ": 2 * MAX_SYMBOLS + 8,
            "NYSE": 2,
            "NYSE American": 1,
            "NYSE Arca": 0,
            "Cboe": 0,
        }
    restrictions = restrictions or (
        "ticker,exchange,restriction\nSELL,NASDAQ,sell_only\nSTOP,NASDAQ,buy_suspended\n"
    )
    snapshot = parse_rakuten_ordinary_snapshot(
        catalog,
        catalog_mode="cash_ordinary_tradable",
        effective_session=_MON,
        catalog_received_at=clock + timedelta(minutes=1),
        reported_count=len(catalog.splitlines()) - 1,
        exclusions_csv=restrictions,
        exclusions_received_at=clock + timedelta(minutes=2),
        exclusions_reported_count=len(restrictions.splitlines()) - 1,
    )
    current = read_current_listing_master(store)["US"]
    ids = {
        row.key: row.instrument_id
        for row in current.slice.rows
        if row.instrument_id is not None and row.listing_state == "listed"
    }
    bindings = (
        RakutenListingBinding(key, ids[key], row)
        for row in (*snapshot.rows, *snapshot.restriction_only)
        if (key := rakuten_listing_key(row.ticker, row.exchange)) in ids
    )
    assert (
        refresh_current_us_rakuten_scope(
            store,
            snapshot=snapshot,
            catalog_venue_counts=venue_counts,
            bindings=bindings,
            expected_session=_MON,
            cutoff=_CUTOFF,
        )
        == "updated"
    )
    return read_current_listing_master(store)["US"]


def _plan(current, *, price_session=_FRI, cutoff=_CUTOFF, adjustment="raw"):
    return plan_current_us_daily_prices(
        current,
        today_open=True,
        price_session=price_session,
        cutoff=cutoff,
        adjustment=adjustment,
    )


def test_complete_current_intersection_plans_every_eligible_symbol_for_previous_price_session():
    current = _attach(_store())
    plan = _plan(current)
    assert plan is not None
    assert (plan.listing_session, plan.price_session) == (_MON, _FRI)
    assert [len(batch.query.symbols) for batch in plan.batches] == [MAX_SYMBOLS, MAX_SYMBOLS, 2]
    assert tuple(target.listing_key[1] for target in plan.targets) == _ESTABLISHED
    ids = {row.key: row.instrument_id for row in current.slice.rows}
    assert all(ids[target.listing_key] == target.instrument_id for target in plan.targets)
    assert tuple(key for batch in plan.batches for key in batch.expected) == tuple(
        (symbol, _FRI) for symbol in _ESTABLISHED
    )
    for batch in plan.batches:
        params = parse_qs(urlsplit(batch.query.url()).query)
        assert params["feed"] == ["sip"]
        assert params["currency"] == ["USD"]
        assert params["asof"] == ["-"]
        assert params["adjustment"] == ["raw"]


def test_same_day_price_is_distinct_from_older_symbol_instance_or_old_venue():
    current = _attach(_store())
    plan = _plan(current, price_session=_MON, adjustment="split")
    assert plan is not None
    assert {symbol for batch in plan.batches for symbol in batch.query.symbols} == {
        *_ESTABLISHED,
        "IPO",
        "REUSE",
        "WMT",
    }
    assert all(
        batch.query.adjustment == "split" and batch.session_dates == (_MON,)
        for batch in plan.batches
    )
    assert any(target.listing_key == ("XNAS", "WMT") for target in plan.targets)
    assert not any(target.listing_key == ("XNYS", "WMT") for target in plan.targets)


def test_absent_stale_wrong_identity_or_only_restricted_scope_is_not_an_empty_success():
    store = _store()
    assert _plan(None) is None
    assert _plan(read_current_listing_master(store)["US"]) is None
    current = _attach(
        store,
        catalog="ticker,exchange\nSELL,NASDAQ\n",
        venue_counts={"NASDAQ": 1, "NYSE": 0, "NYSE American": 0, "NYSE Arca": 0, "Cboe": 0},
        restrictions="ticker,exchange,restriction\nSELL,NASDAQ,sell_only\n",
    )
    assert _plan(current) is None
    healthy = _attach(_store())
    assert _plan(healthy, cutoff=_MON_CLOCK) is None
    assert _plan(healthy, cutoff=_CUTOFF + timedelta(days=1)) is None
    assert healthy.rakuten is not None
    first, *remaining = healthy.rakuten.rows
    mismatched = replace(
        healthy.rakuten, rows=(replace(first, instrument_id="wrong-id"), *remaining)
    )
    assert _plan(replace(healthy, rakuten=mismatched)) is None


def test_session_arguments_do_not_relabel_future_price_or_stale_scope():
    current = _attach(_store())
    with pytest.raises(ValueError, match="price session cannot follow"):
        _plan(current, price_session=_MON + timedelta(days=1))
    assert (
        plan_current_us_daily_prices(
            current, today_open=False, price_session=_FRI, cutoff=_CUTOFF, adjustment="raw"
        )
        is None
    )
