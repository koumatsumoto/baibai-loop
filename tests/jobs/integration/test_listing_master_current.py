from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    ListingMarket,
    ObservedListing,
    VerifiedListingEvent,
)
from baibai_engine.storage.objects.json import MAX_PRIVATE_OBJECT_BYTES
from baibai_engine.storage.objects.r2 import LakePublishError, RemoteObject
from baibai_jobs.integration.listing_master_current import (
    LISTING_MASTER_CURRENT_KEY,
    ListingUpdateStatus,
    read_current_listing_master,
    refresh_current_listing_master,
)

_JP = ObservedListing(("XTKS", "72030"), "合成自動車", "domestic_share")
_US = ObservedListing(("XNAS", "AAA"), "Synthetic USD Class A", "not_flagged_fund")
_FUND = ObservedListing(("XNYS", "FUND"), "Synthetic listed fund", "fund")
_DAY = date(2026, 11, 27)
_AT = datetime(2026, 11, 27, 23, tzinfo=UTC)
_ORIGIN = {"JP": "jquants-jpx", "US": "nasdaq-trader"}


class _Store(MemoryR2Store):
    def __init__(self) -> None:
        super().__init__()
        self.before_put: Callable[[], None] | None = None
        self.conditions: list[tuple[str | None, bool]] = []

    def get_checked_object(self, key: str, *, max_bytes: int) -> tuple[bytes, RemoteObject] | None:
        response = self.get_bytes_with_metadata(key)
        if response is None:
            return None
        body, metadata = response
        if not 0 < len(body) <= max_bytes or (
            hashlib.sha256(body).hexdigest() != metadata.metadata["sha256"]
        ):
            raise LakePublishError("fail checked object fixture")
        return body, metadata

    def put_file(self, key, path, *, if_match=None, if_none_match=False, **fields):
        self.conditions.append((if_match, if_none_match))
        before, self.before_put = self.before_put, None
        if before is not None:
            before()
        return super().put_file(key, path, if_match=if_match, if_none_match=if_none_match, **fields)


def _refresh(
    store: _Store,
    market: ListingMarket,
    observations: Iterable[ObservedListing],
    *,
    day: date = _DAY,
    received: datetime = _AT,
    complete: bool = True,
    events: tuple[VerifiedListingEvent, ...] = (),
) -> ListingUpdateStatus:
    return refresh_current_listing_master(
        store,
        market=market,
        session=day,
        source=_ORIGIN[market],
        received_at=received,
        observations=observations,
        complete=complete,
        verified_events=events,
    )


def test_bootstraps_and_reads_one_file_including_listings_not_in_screening():
    store = _Store()
    assert read_current_listing_master(store) == {}
    scheduled = VerifiedListingEvent(
        "listed", ("XNAS", "NEXT"), _DAY + timedelta(days=3), "official notice fixture"
    )
    assert _refresh(store, "US", (_US, _FUND), events=(scheduled,)) == "updated"
    assert store.conditions == [(None, True)]
    us = read_current_listing_master(store)["US"]

    assert _refresh(store, "JP", (_JP,), received=_AT - timedelta(hours=12)) == "updated"
    state = read_current_listing_master(store)
    assert state["US"] == us
    assert state["JP"].slice.rows[0].key == _JP.key
    assert state["JP"].received_at < state["US"].received_at
    assert (state["JP"].source, us.source) == ("jquants-jpx", "nasdaq-trader")
    assert us.received_count == 2
    assert {row.key: (row.listing_state, row.issue_type) for row in us.slice.rows} == {
        _US.key: ("listed", "not_flagged_fund"),
        _FUND.key: ("listed", "fund"),
        scheduled.key: ("scheduled", None),
    }
    assert set(store.values) == {LISTING_MASTER_CURRENT_KEY}
    assert not store.delete_keys


def test_stale_and_exact_same_input_never_rewrite_the_file():
    store = _Store()
    assert _refresh(store, "US", (_US, _FUND)) == "updated"
    body = store.values[LISTING_MASTER_CURRENT_KEY].body
    assert _refresh(store, "US", (_FUND, _US)) == "unchanged"
    assert (
        _refresh(
            store, "US", (_US,), received=_AT + timedelta(hours=1), day=_DAY - timedelta(days=1)
        )
        == "stale"
    )
    assert _refresh(store, "US", (_US,), received=_AT - timedelta(minutes=1)) == "stale"
    with pytest.raises(ValueError, match="same market receipt"):
        _refresh(store, "US", (_US,))

    assert store.values[LISTING_MASTER_CURRENT_KEY].body == body
    assert store.put_keys == [LISTING_MASTER_CURRENT_KEY]


def test_partial_market_update_preserves_both_clocks_but_does_not_block_other_market():
    store = _Store()
    # Authenticated publication's unrelated Machine pointer remains exactly untouched.
    machine_key = "lake/pointers/market/current.json"
    store.store_object(machine_key, b"fixture Machine pointer")
    assert _refresh(store, "JP", (_JP,)) == "updated"
    assert _refresh(store, "US", (_US,)) == "updated"
    previous = read_current_listing_master(store)
    next_day = _DAY + timedelta(days=3)
    next_time = _AT + timedelta(days=3)
    with pytest.raises(ValueError, match="incomplete listing capture"):
        _refresh(store, "JP", (), day=next_day, received=next_time, complete=False)
    assert read_current_listing_master(store) == previous
    assert _refresh(store, "US", (_US,), day=next_day, received=next_time) == "updated"
    current = read_current_listing_master(store)
    assert current["JP"] == previous["JP"]
    assert current["US"].slice.session == next_day
    assert current["US"].received_at == next_time
    assert store.values[machine_key].body == b"fixture Machine pointer"
    assert set(store.values) == {machine_key, LISTING_MASTER_CURRENT_KEY}
    persisted = json.loads(store.values[LISTING_MASTER_CURRENT_KEY].body)
    assert set(persisted["US"]) == {"slice", "source", "received_at", "received_count"}
    assert "changes" not in persisted["US"]["slice"]


@pytest.mark.parametrize("already_exists", [False, True])
def test_cas_retries_both_old_etag_and_first_create_without_losing_other_market(already_exists):
    store = _Store()
    if already_exists:
        assert _refresh(store, "US", (_US,)) == "updated"

    def competing_jp_update() -> None:
        assert _refresh(store, "JP", (_JP,)) == "updated"

    store.before_put = competing_jp_update
    assert _refresh(store, "US", (_US, _FUND), received=_AT + timedelta(minutes=1)) == "updated"
    state = read_current_listing_master(store)
    assert state["JP"].slice.rows[0].key == _JP.key
    assert state["US"].received_count == 2
    assert len(store.conditions) == 3 + int(already_exists)
    assert store.conditions[-3] == store.conditions[-2]
    assert store.conditions[-1][0] != store.conditions[-2][0]
    assert not store.conditions[-1][1]
    assert set(store.values) == {LISTING_MASTER_CURRENT_KEY}


def test_same_market_cas_retry_reconciles_with_the_newly_committed_identity():
    store = _Store()
    result = read_current_listing_master(store)
    assert "US" not in result

    def competing_older_us_update() -> None:
        assert (
            _refresh(
                store, "US", (_US,), day=_DAY - timedelta(days=1), received=_AT - timedelta(days=1)
            )
            == "updated"
        )

    store.before_put = competing_older_us_update
    assert _refresh(store, "US", (_US,)) == "updated"
    winner = read_current_listing_master(store)["US"]
    assert winner.slice.session == _DAY
    assert winner.slice.rows[0].identity_since == _DAY - timedelta(days=1)


def test_shared_object_handles_two_full_sized_master_slices_not_marker_sized():
    store = _Store()
    jp = tuple(
        ObservedListing(("XTKS", str(10000 + index)), f"国内上場合成企業 {index}", "domestic_share")
        for index in range(3800)
    )
    us = tuple(
        ObservedListing(
            ("XNAS" if index < 5600 else "XNYS", f"US{index:04}"),
            f"Synthetic directly listed corporation class A {index}",
            "not_flagged_fund",
        )
        for index in range(8300)
    )
    assert _refresh(store, "JP", jp) == "updated"
    assert _refresh(store, "US", us) == "updated"
    assert set(store.values) == {LISTING_MASTER_CURRENT_KEY}
    assert len(store.values[LISTING_MASTER_CURRENT_KEY].body) > MAX_PRIVATE_OBJECT_BYTES
    current = read_current_listing_master(store)
    assert len(current["JP"].slice.rows) == len(jp)
    assert len(current["US"].slice.rows) == len(us)


def test_in_file_market_mismatch_is_not_repaired_by_overwriting_another_market():
    store = _Store()
    assert _refresh(store, "JP", (_JP,)) == "updated"
    pp = store.values[LISTING_MASTER_CURRENT_KEY]
    native = json.loads(pp.body)
    store.store_object(LISTING_MASTER_CURRENT_KEY, json.dumps({"US": native["JP"]}).encode())
    with pytest.raises(ValueError, match="market sections"):
        _refresh(store, "US", (_US,))
    assert store.put_keys == [LISTING_MASTER_CURRENT_KEY]


def test_unknown_nested_schema_cannot_remove_another_market_field():
    store = _Store()
    assert _refresh(store, "JP", (_JP,)) == "updated"
    body = json.loads(store.values[LISTING_MASTER_CURRENT_KEY].body)
    body["JP"]["slice"]["rows"][0]["future_jp_metadata"] = "fixture"
    native = json.dumps(body).encode()
    store.store_object(LISTING_MASTER_CURRENT_KEY, native)
    with pytest.raises(ValueError, match="future_jp_metadata"):
        _refresh(store, "US", (_US,))
    assert store.values[LISTING_MASTER_CURRENT_KEY].body == native
    assert store.put_keys == [LISTING_MASTER_CURRENT_KEY]
