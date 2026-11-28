from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    ObservedListing,
    VerifiedListingEvent,
)
from baibai_jobs.integration.listing_master_current import (
    LISTING_MASTER_CURRENT_KEY,
    read_current_listing_master,
    refresh_current_listing_master,
)
from baibai_jobs.integration.us_listing_refresh import (
    USListingDirectoryReceipt,
    USListingInputs,
    refresh_current_us_listings,
)

_DAY = date(2026, 11, 27)
_CREATED = datetime(2026, 11, 27, 13, 43, tzinfo=UTC)
_RECEIVED = _CREATED + timedelta(minutes=5)
_CHECKED = _RECEIVED + timedelta(minutes=2)
_NASDAQ = (
    b"Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
    b"GOOG|Fixture - Class C Common Stock|Q|N|N|100|N|N\n"
    b"File Creation Time: 1127202608:47|||||||\n"
)
_NYSE = (
    b"ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
    b"BRK.B|Fixture - Class B Common Stock|N|BRK.B|N|100|N|BRKB\n"
    b"File Creation Time: 1127202608:47|||||||\n"
)


class _Store(MemoryR2Store):
    def get_checked_object(self, key, *, max_bytes):
        result = self.get_bytes_with_metadata(key)
        if result is not None:
            assert len(result[0]) <= max_bytes
        return result


def _inputs() -> USListingInputs:
    return USListingInputs(
        (
            USListingDirectoryReceipt("nasdaqlisted.txt", _DAY, _RECEIVED, True, _NASDAQ),
            USListingDirectoryReceipt("otherlisted.txt", _DAY, _RECEIVED, True, _NYSE),
        )
    )


def _refresh(store, inputs, *, now=lambda: _CHECKED, calendar=None):
    def load(target: date) -> USListingInputs:
        assert target == _DAY
        return inputs

    return refresh_current_us_listings(
        store,
        created_at=_CREATED,
        now=now,
        scheduled=True,
        is_open={_DAY: True}.get if calendar is None else calendar,
        load_verified=load,
    )


def test_complete_both_exchange_files_refresh_only_us_and_repeat_without_a_write():
    store = _Store()
    assert (
        refresh_current_listing_master(
            store,
            market="JP",
            session=_DAY,
            source="jquants-jpx",
            received_at=_CREATED,
            observations=(ObservedListing(("XTKS", "72030"), "JP fixture", "ordinary"),),
            complete=True,
        )
        == "updated"
    )
    original_jp = read_current_listing_master(store)["JP"]
    assert _refresh(store, _inputs()) == "updated"
    current = read_current_listing_master(store)
    assert current["JP"] == original_jp
    us = current["US"]
    assert (us.slice.session, us.received_at, us.source, us.received_count) == (
        _DAY,
        _RECEIVED,
        "nasdaq-trader",
        2,
    )
    assert {row.key for row in us.slice.rows} == {("XNAS", "GOOG"), ("XNYS", "BRK.B")}
    assert _refresh(store, _inputs()) == "unchanged"
    assert store.put_keys == [LISTING_MASTER_CURRENT_KEY, LISTING_MASTER_CURRENT_KEY]
    assert not store.delete_keys


def test_verified_closure_skips_and_unknown_calendar_errors_before_loading_or_writing():
    def unexpected_load(_: date) -> USListingInputs:
        pytest.fail("non-session must not touch provider input")

    store = _Store()
    for calendar in ({_DAY: False}, {}):
        if calendar:
            assert (
                refresh_current_us_listings(
                    store,
                    created_at=_CREATED,
                    now=lambda: _CHECKED,
                    scheduled=True,
                    is_open=calendar.get,
                    load_verified=unexpected_load,
                )
                == "closed"
            )
        else:
            with pytest.raises(ValueError, match="verified US market calendar"):
                refresh_current_us_listings(
                    store,
                    created_at=_CREATED,
                    now=lambda: _CHECKED,
                    scheduled=True,
                    is_open=calendar.get,
                    load_verified=unexpected_load,
                )
    assert not store.get_keys
    assert not store.put_keys


@pytest.mark.parametrize(
    "changes",
    [
        {"source_session": _DAY - timedelta(days=1)},
        {"received_at": _RECEIVED - timedelta(days=1)},
        {"received_at": _CHECKED + timedelta(seconds=1)},
        {"received_at": _RECEIVED.replace(tzinfo=None)},
        {"complete": False},
        {"file": "nasdaqlisted.txt", "body": _NASDAQ},
        {"body": b"not the native file"},
    ],
)
def test_invalid_or_noncurrent_receipts_never_reach_the_store(changes):
    store = _Store()
    first, second = _inputs().receipts
    with pytest.raises(ValueError, match=r"US listing|symbol director"):
        _refresh(store, USListingInputs((first, replace(second, **changes))))
    assert not store.get_keys
    assert not store.put_keys


@pytest.mark.parametrize(
    "event",
    [
        VerifiedListingEvent("listed", ("XTKS", "12340"), _DAY, "out of scope"),
        VerifiedListingEvent(
            "moved", ("XNAS", "GOOG"), _DAY, "out of scope", previous_key=("XTKS", "12340")
        ),
    ],
)
def test_foreign_mic_never_reaches_us_storage(event):
    store = _Store()
    with pytest.raises(ValueError, match="outside the XNAS/XNYS scope"):
        _refresh(store, replace(_inputs(), verified_events=(event,)))
    assert not store.get_keys
    assert not store.put_keys


def test_date_rollover_after_loading_does_not_publish_old_receipts():
    store = _Store()
    checks = iter((_CHECKED, datetime(2026, 11, 28, 5, tzinfo=UTC)))
    with pytest.raises(ValueError, match="no longer for the current New York date"):
        _refresh(store, _inputs(), now=lambda: next(checks))
    assert not store.get_keys
    assert not store.put_keys
