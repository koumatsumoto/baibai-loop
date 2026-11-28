from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from tests.jobs.integration.test_listing_master_current import _AT, _DAY, _JP, _US, _refresh, _Store

from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    ListingKey,
    ObservedListing,
    VerifiedListingEvent,
)
from baibai_engine.machine_analysis.observations.stocks.us_universe import (
    RakutenOrdinarySnapshot,
    parse_rakuten_ordinary_snapshot,
)
from baibai_jobs.integration.listing_master_current import (
    LISTING_MASTER_CURRENT_KEY,
    RakutenListingBinding,
    read_current_listing_master,
    read_current_rakuten_scope,
    refresh_current_us_rakuten_scope,
)

_BW = ObservedListing(("XNYS", "BW"), "Synthetic BW", "not_flagged_fund")
_CLASS = ObservedListing(("XNYS", "BRK.B"), "Synthetic separate Class B", "not_flagged_fund")
_CATALOG = "ticker,exchange\nAAA,NASDAQ\nBRK B,NYSE\n"
_RESTRICTIONS = "ticker,exchange,restriction\n"
_EXCLUDED = _RESTRICTIONS + "AAA,NASDAQ,buy_suspended\nBW,NYSE,sell_only\n"
_CATALOG_AT = _AT + timedelta(minutes=5)
_EXCLUSIONS_AT = _AT + timedelta(minutes=10)
_CUTOFF = _AT + timedelta(minutes=45)


def _store() -> _Store:
    store = _Store()
    assert _refresh(store, "JP", (_JP,)) == "updated"
    assert _refresh(store, "US", (_US, _BW, _CLASS)) == "updated"
    return store


def _snapshot(
    *,
    exclusions: str | None = _EXCLUDED,
    catalog_at=_CATALOG_AT,
    exclusions_at=_EXCLUSIONS_AT,
) -> RakutenOrdinarySnapshot:
    return parse_rakuten_ordinary_snapshot(
        _CATALOG,
        catalog_mode="cash_ordinary_tradable" if exclusions else "cash_ordinary_catalog",
        effective_session=_DAY,
        catalog_received_at=catalog_at,
        reported_count=2,
        exclusions_csv=exclusions,
        exclusions_received_at=exclusions_at if exclusions else None,
        exclusions_reported_count=None if exclusions is None else len(exclusions.splitlines()) - 1,
    )


def _bindings(store: _Store, capture: RakutenOrdinarySnapshot):
    current = read_current_listing_master(store)["US"].slice
    ids = {row.key: row.instrument_id for row in current.rows}
    # Fixture stands in for the separate broker-to-exchange join, including the exact class.
    mapping: dict[str, ListingKey] = {
        "AAA": _US.key,
        "BRK B": _CLASS.key,
        "BW": _BW.key,
    }
    return tuple(
        RakutenListingBinding(mapping[row.ticker], ids[mapping[row.ticker]], row)
        for row in (*capture.rows, *capture.restriction_only)
    )


def _attach(store, capture, bindings=None, *, cutoff=_CUTOFF):
    if bindings is None:
        bindings = _bindings(store, capture) if capture is not None else ()
    return refresh_current_us_rakuten_scope(
        store,
        snapshot=capture,
        bindings=bindings,
        expected_session=_DAY,
        cutoff=cutoff,
    )


def _read(store, *, session=_DAY, cutoff=_CUTOFF):
    return read_current_rakuten_scope(
        read_current_listing_master(store).get("US"),
        expected_session=session,
        cutoff=cutoff,
    )


def test_attachment_writes_only_us_broker_facts_and_keeps_restriction_only_identity():
    store = _store()
    before = read_current_listing_master(store)
    capture = _snapshot()
    assert _attach(store, capture) == "updated"
    after = read_current_listing_master(store)
    assert replace(after["US"], rakuten=None) == before["US"]
    assert after["JP"] == before["JP"]
    assert set(store.values) == {LISTING_MASTER_CURRENT_KEY}
    native = json.loads(store.values[LISTING_MASTER_CURRENT_KEY].body)
    assert "rakuten" not in native["JP"]
    scope = _read(store)
    assert scope is not None
    assert scope.effective_session == _DAY
    assert (scope.catalog_received_at, scope.exclusions_received_at) == (
        _CATALOG_AT,
        _EXCLUSIONS_AT,
    )
    assert (scope.catalog_count, scope.exclusions_count) == (2, 2)
    assert {(row.listing_key, row.broker_symbol, row.state) for row in scope.rows} == {
        (_US.key, "AAA", "buy_suspended"),
        (_CLASS.key, "BRK B", "catalog_target"),
        (_BW.key, "BW", "sell_only"),
    }
    assert _attach(store, capture, reversed(_bindings(store, capture))) == "unchanged"
    assert store.put_keys == [LISTING_MASTER_CURRENT_KEY] * 3
    assert not store.delete_keys


@pytest.mark.parametrize("mode", ["missing", "catalog_only", "future"])
def test_incomplete_or_future_pair_cannot_remove_a_known_restriction(mode):
    store = _store()
    assert _attach(store, _snapshot()) == "updated"
    before = store.values[LISTING_MASTER_CURRENT_KEY]
    value = (
        None
        if mode == "missing"
        else _snapshot(
            exclusions=None if mode == "catalog_only" else _RESTRICTIONS,
            exclusions_at=_CUTOFF + timedelta(minutes=1),
        )
    )
    with pytest.raises(ValueError, match=r"complete Rakuten|after the cutoff"):
        _attach(store, value)
    assert store.values[LISTING_MASTER_CURRENT_KEY] == before
    assert any(row.state == "sell_only" for row in _read(store).rows)
    assert store.put_keys == [LISTING_MASTER_CURRENT_KEY] * 3


@pytest.mark.parametrize(
    ("catalog_delta", "exclusions_delta"),
    [(2, -1), (-1, 2), (2, 0)],
)
def test_one_old_or_same_restriction_clock_cannot_clear_negative_pair(
    catalog_delta, exclusions_delta
):
    store = _store()
    assert _attach(store, _snapshot()) == "updated"
    before = store.values[LISTING_MASTER_CURRENT_KEY]
    candidate = _snapshot(
        exclusions=_RESTRICTIONS,
        catalog_at=_CATALOG_AT + timedelta(minutes=catalog_delta),
        exclusions_at=_EXCLUSIONS_AT + timedelta(minutes=exclusions_delta),
    )
    assert _attach(store, candidate) == "stale"
    assert store.values[LISTING_MASTER_CURRENT_KEY] == before


def test_authoritative_new_empty_exclusions_can_clear_known_restrictions():
    store = _store()
    assert _attach(store, _snapshot()) == "updated"
    new_pair = _snapshot(
        exclusions=_RESTRICTIONS, exclusions_at=_EXCLUSIONS_AT + timedelta(minutes=1)
    )
    assert _attach(store, new_pair) == "updated"
    scope = _read(store)
    assert scope.exclusions_count == 0
    assert {row.listing_key for row in scope.rows} == {_CLASS.key, _US.key}
    assert all(row.state == "catalog_target" for row in scope.rows)


def test_stale_receipt_session_or_cutoff_is_not_eligible_and_stays_in_storage():
    store = _store()
    assert _attach(store, _snapshot()) == "updated"
    original = _read(store)
    assert _read(store, cutoff=_EXCLUSIONS_AT - timedelta(seconds=1)) is None
    assert _read(store, cutoff=_EXCLUSIONS_AT) == original
    next_day = _DAY + timedelta(days=3)
    new_time = _AT + timedelta(days=3)
    assert _read(store, cutoff=new_time) is None
    assert _refresh(store, "US", (_US, _BW, _CLASS), day=next_day, received=new_time) == "updated"
    assert read_current_listing_master(store)["US"].rakuten == original
    assert _read(store, cutoff=new_time) is None
    assert _read(store, session=next_day, cutoff=new_time) is None


def test_broker_cas_never_follows_an_exchange_key_reused_by_a_new_security():
    store = _store()
    old_capture = _snapshot()
    assert _attach(store, old_capture) == "updated"
    previous = _read(store)
    advanced = _snapshot(
        catalog_at=_CATALOG_AT + timedelta(minutes=1),
        exclusions_at=_EXCLUSIONS_AT + timedelta(minutes=1),
    )
    binding = _bindings(store, advanced)

    def competing_exchange_reuse():
        event = VerifiedListingEvent("reused", _US.key, _DAY, "verified fixture")
        different = replace(_US, security_name="Different security on the same key")
        assert (
            _refresh(
                store,
                "US",
                (different, _CLASS, _BW),
                received=_AT + timedelta(minutes=25),
                events=(event,),
            )
            == "updated"
        )

    store.before_put = competing_exchange_reuse
    assert _attach(store, advanced, binding) == "stale"
    assert read_current_listing_master(store)["US"].rakuten == previous
    assert _read(store) is None  # Exact remaining IDs no longer prove the whole intersection.
    assert set(store.values) == {LISTING_MASTER_CURRENT_KEY}


def test_broker_cas_keeps_independently_updated_jp_and_does_not_create_us_without_listings():
    empty = _Store()
    assert _attach(empty, _snapshot(), ()) == "stale"
    assert empty.values == {}

    store = _store()
    later = _AT + timedelta(minutes=1)
    store.before_put = lambda: _refresh(store, "JP", (_JP,), received=later)
    assert _attach(store, _snapshot()) == "updated"
    state = read_current_listing_master(store)
    assert state["JP"].received_at == later
    assert state["JP"].rakuten is None
    assert state["US"].received_at == _AT


def test_broker_binding_must_reference_actual_normalized_source_observation():
    store = _store()
    source = _snapshot()
    bindings = _bindings(store, source)
    unobserved = replace(bindings[0], observation=replace(source.rows[0], buy_status="buyable"))
    with pytest.raises(ValueError, match="normalized Rakuten observation"):
        _attach(store, source, (unobserved,))
    assert _read(store) is None
    assert store.put_keys == [LISTING_MASTER_CURRENT_KEY] * 2


def test_new_complete_pair_cannot_drop_a_still_observed_known_restriction():
    store = _store()
    assert _attach(store, _snapshot()) == "updated"
    before = store.values[LISTING_MASTER_CURRENT_KEY]
    latest = _snapshot(exclusions_at=_EXCLUSIONS_AT + timedelta(minutes=1))
    incomplete_bindings = [b for b in _bindings(store, latest) if b.listing_key != _BW.key]
    with pytest.raises(ValueError, match="cover all active matched"):
        _attach(store, latest, incomplete_bindings)
    assert store.values[LISTING_MASTER_CURRENT_KEY] == before


def test_genuine_source_status_cannot_be_swapped_to_a_different_active_instrument():
    store = _store()
    capture = _snapshot()
    original = _bindings(store, capture)
    by_key = {binding.listing_key: binding for binding in original}
    wrong = tuple(
        replace(binding, observation=by_key[_BW.key].observation)
        if binding.listing_key == _CLASS.key
        else replace(binding, observation=by_key[_CLASS.key].observation)
        if binding.listing_key == _BW.key
        else binding
        for binding in original
    )
    with pytest.raises(ValueError, match="exact Rakuten listing identity"):
        _attach(store, capture, wrong)
    assert _read(store) is None
    assert store.put_keys == [LISTING_MASTER_CURRENT_KEY] * 2


def test_an_omitted_first_active_binding_cannot_make_a_falsely_ready_scope():
    store = _store()
    with pytest.raises(ValueError, match="cover all active matched"):
        _attach(store, _snapshot(), ())
    assert _read(store) is None
    assert store.put_keys == [LISTING_MASTER_CURRENT_KEY] * 2


def test_an_unsupported_source_row_has_no_expectation_to_be_attached():
    store = _store()
    fixture = _snapshot()
    captured = parse_rakuten_ordinary_snapshot(
        _CATALOG + "OTHER,OTC\n",
        catalog_mode="cash_ordinary_tradable",
        effective_session=_DAY,
        catalog_received_at=_CATALOG_AT,
        reported_count=3,
        exclusions_csv=_EXCLUDED,
        exclusions_received_at=_EXCLUSIONS_AT,
        exclusions_reported_count=2,
    )
    assert _attach(store, captured, _bindings(store, fixture)) == "updated"
    scope = _read(store)
    assert scope.catalog_count == 3
    assert {row.broker_symbol for row in scope.rows} == {"AAA", "BW", "BRK B"}


def test_existing_broker_scope_does_not_authorize_a_newly_active_same_day_listing():
    store = _store()
    announced = ObservedListing(("XNAS", "NEXT"), "Recognized after capture", "not_flagged_fund")
    initial = _snapshot()
    capture = parse_rakuten_ordinary_snapshot(
        _CATALOG + "NEXT,NASDAQ\n",
        catalog_mode="cash_ordinary_tradable",
        effective_session=_DAY,
        catalog_received_at=_CATALOG_AT,
        reported_count=3,
        exclusions_csv=_EXCLUDED,
        exclusions_received_at=_EXCLUSIONS_AT,
        exclusions_reported_count=2,
    )
    bound = _bindings(store, initial)
    assert _attach(store, capture, bound) == "updated"
    previous = _read(store)
    assert previous is not None
    assert previous.catalog_count == 3
    assert len(previous.rows) == 3
    assert (
        _refresh(store, "US", (_US, _BW, _CLASS, announced), received=_AT + timedelta(minutes=20))
        == "updated"
    )
    assert _read(store) is None
    stored = read_current_listing_master(store)["US"]
    assert stored.rakuten == previous  # Preserve evidence internally, not as a qualified scope.
    current_id = next(row.instrument_id for row in stored.slice.rows if row.key == announced.key)
    assert current_id is not None
    new_pair = replace(capture, catalog_received_at=_AT + timedelta(minutes=21))
    newly_bound = RakutenListingBinding(announced.key, current_id, capture.rows[-1])
    assert _attach(store, new_pair, (*bound, newly_bound)) == "updated"
    current = _read(store)
    assert current is not None
    assert current.active_listing_sha256 != previous.active_listing_sha256
    assert (
        next(row for row in current.rows if row.listing_key == announced.key).state
        == "catalog_target"
    )


def test_refresh_without_supported_active_identity_change_keeps_scope_current():
    store = _store()
    assert _attach(store, _snapshot()) == "updated"
    previous = _read(store)
    unrelated = ObservedListing(("XASE", "OTHER"), "Outside US broker matching", "not_flagged_fund")
    notice = VerifiedListingEvent(
        "listed", ("XNAS", "FUTURE"), _DAY + timedelta(days=3), "official fixture"
    )
    assert (
        _refresh(
            store,
            "US",
            (_US, _BW, _CLASS, unrelated),
            received=_AT + timedelta(minutes=20),
            events=(notice,),
        )
        == "updated"
    )
    assert _read(store) == previous
