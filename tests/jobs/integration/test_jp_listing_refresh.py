"""Native owners to current JP slice; all R2 operations are in the fake."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.screening_sqlite import make_master_records

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    CalendarAttempt,
    CalendarL1,
)
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.master_native import (
    ExactMasterReceipt,
    MasterAttempt,
    MasterL1,
    resolved_master_requests,
)
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    ObservedListing,
    VerifiedListingEvent,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    NASDAQ_DIRECTORY_SOURCE,
)
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.integration.jp_listing_refresh import refresh_current_jp_listings
from baibai_jobs.integration.listing_master_current import (
    LISTING_MASTER_CURRENT_KEY,
    NasdaqDirectoryVersion,
    read_current_listing_master,
    refresh_current_listing_master,
)

_DAY = date(2026, 11, 27)
_PREV = date(2026, 11, 26)
_EARLY = datetime(2026, 11, 26, 8, 43, tzinfo=UTC)  # provider can supply the named next session
_AT = datetime(2026, 11, 27, 8, 43, tzinfo=UTC)
_CAL = datetime(2026, 11, 26, 7, 0, tzinfo=UTC)


class _Store(MemoryR2Store):
    def get_checked_object(self, key, *, max_bytes):
        result = self.get_bytes_with_metadata(key)
        if result is not None and len(result[0]) > max_bytes:
            raise LakePublishError("invalid checked object fixture")
        return result


def _calendar(owner, *, day=_DAY, at=_CAL, division="1"):
    iso = day.isoformat()
    p = JQuantsPage(
        "market_calendar",
        {"from": iso, "to": iso},
        at,
        json.dumps({"data": [{"Date": iso, "HolDiv": division}]}).encode(),
    )
    owner.commit([CalendarAttempt(f"{day}-{at}", day, day, None, (p,))])


def _master(identity, *, day=_DAY, at=_EARLY, rows=None, partial=False):
    if rows is None:
        rows = make_master_records(day)
        rows[0].update(Mkt="0111", S33="7100", S33Nm="証券･商品先物取引業")
        rows.append(dict(rows[0], Code="10005", CoName="別種優先株"))
    iso = day.isoformat()
    first = JQuantsPage(
        "master",
        {"date": iso},
        at - timedelta(seconds=1),
        json.dumps({"data": rows[:1800], "pagination_key": "genuine"}).encode(),
    )
    last = JQuantsPage(
        "master",
        {"date": iso, "pagination_key": "genuine"},
        at,
        json.dumps({"data": rows[1800:]}).encode(),
    )
    return MasterAttempt(identity, day, None, (first,) if partial else (first, last))


def _owners(tmp_path):
    store = _Store()
    calendar = CalendarL1(store, tmp_path / "calendar")
    master = MasterL1(store, tmp_path / "master")
    calendar.initialize()
    master.initialize()
    store.store_object("lake/pointers/market/current.json", b"unchanged Machine pointer")
    return store, calendar, master


def _refresh(store, calendar, master, *, day=_DAY, evaluated_at=_AT, events=()):
    fixed = master.fixed()
    resolved = list(resolved_master_requests(store, fixed, days=(day,)))
    return refresh_current_jp_listings(
        store,
        session=day,
        evaluated_at=evaluated_at,
        calendar=calendar.fixed(),
        master=ExactMasterReceipt(fixed, resolved[0][1]) if resolved else None,
        verified_events=events,
    )


@pytest.mark.parametrize("division", ["1", "2"])
def test_open_cash_session_writes_only_one_current_file_from_exact_native_pages(tmp_path, division):
    store, calendar, master = _owners(tmp_path)
    _calendar(calendar, division=division)
    master.commit([_master("old", day=_PREV)])
    old_pack = master.fixed().value()["receipts"]["old"]["pack"]
    master.commit([_master("selected")])
    selected_pack = master.fixed().value()["receipts"]["selected"]["pack"]
    # Identical next receipt may confirm the head, but cannot rewrite original clock.
    assert master.commit([_master("equal", at=_AT - timedelta(hours=1))])["equal"] == ["equal"]
    us_session = date(2026, 11, 25)
    native_stamp = f"{us_session:%m%d%Y}08:47"
    assert (
        refresh_current_listing_master(
            store,
            market="US",
            session=us_session,
            source=NASDAQ_DIRECTORY_SOURCE,
            received_at=_AT - timedelta(days=2),
            observations=(ObservedListing(("XNAS", "AAA"), "US Name", None),),
            complete=True,
            nasdaq_directories=(
                NasdaqDirectoryVersion("nasdaqlisted.txt", native_stamp, "a" * 64),
                NasdaqDirectoryVersion("otherlisted.txt", native_stamp, "b" * 64),
            ),
        )
        == "updated"
    )
    before_us = read_current_listing_master(store)["US"]
    before = dict(store.values)
    store.get_keys.clear()
    store.put_keys.clear()

    noticed = VerifiedListingEvent(
        "listed", ("XTKS", "56A70"), _DAY + timedelta(days=3), "verified full issue fixture"
    )
    assert _refresh(store, calendar, master, events=(noticed,)) == "updated"
    master_packs = [key for key in store.get_keys if "/master/v1/packs/" in key]
    assert len(master_packs) == 1
    assert selected_pack in master_packs[0]
    assert old_pack not in master_packs[0]
    assert store.put_keys == [LISTING_MASTER_CURRENT_KEY]
    assert not store.delete_keys
    assert {k: v for k, v in store.values.items() if k != LISTING_MASTER_CURRENT_KEY} == {
        k: v for k, v in before.items() if k != LISTING_MASTER_CURRENT_KEY
    }

    listings = read_current_listing_master(store)
    assert listings["US"] == before_us
    assert listings["US"].slice.rows[0].key == ("XNAS", "AAA")
    jp = listings["JP"]
    assert jp.slice.session == _DAY
    assert jp.received_at == _EARLY
    assert jp.received_count == 2501
    assert jp.source == "jquants-equities-master"
    rows = {row.key: row for row in jp.slice.rows}
    assert rows[("XTKS", "10000")].japan.sector_33 == "証券・商品先物取引業"
    assert rows[("XTKS", "10005")].issue_type == "preferred_stock"
    assert rows[noticed.key].listing_state == "scheduled"
    assert _refresh(store, calendar, master, events=(noticed,)) == "unchanged"
    assert store.put_keys == [LISTING_MASTER_CURRENT_KEY]


@pytest.mark.parametrize(
    "case", ["closed", "futures-only", "unknown", "unproved-closure", "future"]
)
def test_calendar_does_not_load_master_or_write_before_proving_open(tmp_path, case):
    store, calendar, _ = _owners(tmp_path)
    division = {"closed": "0", "futures-only": "3", "unknown": "unused"}.get(case, "1")
    _calendar(calendar, division=division)
    fixed = calendar.fixed()
    evaluated_at = _AT
    if case == "unproved-closure":
        head = fixed.value()
        head["days"][_DAY.isoformat()]["division"] = "0"
        fixed = replace(fixed, payload=json.dumps(head).encode())
    if case == "future":
        evaluated_at = _EARLY
    store.put_keys.clear()

    def run():
        return refresh_current_jp_listings(
            store, session=_DAY, evaluated_at=evaluated_at, calendar=fixed, master=None
        )

    if case in {"closed", "futures-only"}:
        assert run() == "closed"
    else:
        with pytest.raises(ValueError, match=r"cannot prove|has not started"):
            run()
    assert not store.put_keys
    assert LISTING_MASTER_CURRENT_KEY not in store.values


@pytest.mark.parametrize(
    ("case", "exception", "match"),
    [
        ("partial", ValueError, "resolved exact-date"),
        ("held", ValueError, "resolved exact-date"),
        ("wrong-day", ValueError, "differs from the selected exact date"),
        ("other-response-date", JQuantsProviderError, "Date does not match"),
        ("future-page", ValueError, "original is later"),
        ("future-confirmation", ValueError, "head is later"),
    ],
)
def test_invalid_native_master_cannot_replace_last_good_current(tmp_path, case, exception, match):
    store, calendar, master = _owners(tmp_path)
    _calendar(calendar, day=_PREV)
    _calendar(calendar)
    master.commit([_master("old", day=_PREV)])
    assert _refresh(store, calendar, master, day=_PREV) == "updated"
    before = store.values[LISTING_MASTER_CURRENT_KEY]
    raw_when = (
        _AT + timedelta(minutes=1) if case in {"future-page", "future-confirmation"} else _EARLY
    )
    rows = make_master_records(_DAY + timedelta(days=3)) if case == "other-response-date" else None
    if case != "wrong-day":
        master.commit([_master("new", at=raw_when, rows=rows, partial=case == "partial")])
    if case == "held":
        changed = make_master_records(_DAY)
        changed[0]["CoName"] = "ambiguous at same timestamp"
        master.commit([_master("held", rows=changed)])
    fixed = master.fixed()
    head = fixed.value()
    if case == "wrong-day":
        head["scopes"][_DAY.isoformat()] = head["scopes"][_PREV.isoformat()]
    if case == "future-page":
        # Even if someone backdates the head clocks, the actual immutable pages are later.
        head["scopes"][_DAY.isoformat()].update(
            confirmed_at=_EARLY.isoformat(),
            last_attempt_at=_EARLY.isoformat(),
            changed_at=_EARLY.isoformat(),
        )
    member_day = _PREV if case == "wrong-day" else _DAY
    resolved = list(resolved_master_requests(store, fixed, days=(member_day,)))
    fixed = replace(fixed, payload=json.dumps(head).encode())
    exact = ExactMasterReceipt(fixed, resolved[0][1]) if resolved else None
    store.put_keys.clear()
    with pytest.raises(exception, match=match):
        refresh_current_jp_listings(
            store,
            session=_DAY,
            evaluated_at=_AT,
            calendar=calendar.fixed(),
            master=exact,
        )
    assert store.values[LISTING_MASTER_CURRENT_KEY] == before
    assert not store.put_keys


def test_delayed_previous_session_never_rolls_back_current_and_notice_requires_exact_class(
    tmp_path,
):
    store, calendar, master = _owners(tmp_path)
    _calendar(calendar)
    _calendar(calendar, day=_PREV)
    master.commit([_master("old", day=_PREV), _master("new")])
    assert _refresh(store, calendar, master) == "updated"
    before = store.values[LISTING_MASTER_CURRENT_KEY]
    store.put_keys.clear()
    assert _refresh(store, calendar, master, day=_PREV) == "stale"
    for key in [("XTKS", "1000"), ("XNAS", "10000"), ("XTKS", "12a45")]:
        notice = VerifiedListingEvent("delisted", key, _DAY, "ambiguous identity fixture")
        with pytest.raises(ValueError, match="exact TSE issue and class"):
            _refresh(store, calendar, master, events=(notice,))
    assert not store.put_keys
    assert store.values[LISTING_MASTER_CURRENT_KEY] == before
