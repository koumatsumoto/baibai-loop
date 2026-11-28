from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    ObservedListing,
    VerifiedListingEvent,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    active_us_listing_ids,
    encode_current_listing_master,
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
    def __init__(self):
        super().__init__()
        self.before_put = None

    def put_file(self, key, path, **kwargs):
        before, self.before_put = self.before_put, None
        if before is not None:
            before()
        return super().put_file(key, path, **kwargs)

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


def test_extending_otherlisted_keeps_verified_existing_ids_and_jp_byte_contents():
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
    assert _refresh(store, _inputs()) == "updated"
    before = read_current_listing_master(store)
    old_ids = active_us_listing_ids(before["US"])
    assert set(old_ids) == {("XNAS", "GOOG"), ("XNYS", "BRK.B")}
    first, second = _inputs().receipts
    extra = (
        b"QAM|Fixture American|A|QAM|N|100|N|QAM\n"
        b"QARCA|Fixture Arca|P|QARCA|Y|100|N|QARCA\n"
        b"QBZX|Fixture BZX|Z|QBZX|N|100|N|QBZX\n"
    )
    second = replace(
        second,
        received_at=second.received_at + timedelta(minutes=1),
        body=second.body.replace(
            b"File Creation Time: 1127202608:47",
            extra + b"File Creation Time: 1127202608:48",
        ),
    )
    expanded = USListingInputs((first, second))
    assert _refresh(store, expanded) == "updated"
    after = read_current_listing_master(store)
    new_ids = active_us_listing_ids(after["US"])
    assert {key: new_ids[key] for key in old_ids} == old_ids
    assert set(new_ids) - set(old_ids) == {("XASE", "QAM"), ("ARCX", "QARCA"), ("BATS", "QBZX")}
    assert encode_current_listing_master({"JP": after["JP"]}) == encode_current_listing_master(
        {"JP": before["JP"]}
    )
    assert _refresh(store, expanded) == "unchanged"


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
    with pytest.raises(ValueError, match="outside the supported US venue scope"):
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


def _capture(nasdaq=_NASDAQ, nyse=_NYSE, *, received_at=_RECEIVED):
    first, second = _inputs().receipts
    return USListingInputs(
        (
            replace(first, body=nasdaq, received_at=received_at),
            replace(second, body=nyse, received_at=received_at),
        )
    )


def _with_stamp(body, version):
    return body.replace(b"1127202608:47", version)


@pytest.mark.parametrize("literal_date", [b"1125202608:47", b"1128202608:47"])
@pytest.mark.parametrize(
    ("replace_nasdaq", "replace_nyse"), [(True, True), (True, False), (False, True)]
)
def test_current_loader_claim_cannot_relabel_a_different_native_date(
    literal_date, replace_nasdaq, replace_nyse
):
    store = _Store()
    nasdaq = _with_stamp(_NASDAQ, literal_date) if replace_nasdaq else _NASDAQ
    nyse = _with_stamp(_NYSE, literal_date) if replace_nyse else _NYSE
    with pytest.raises(ValueError, match="native creation date"):
        _refresh(store, _capture(nasdaq, nyse))
    assert not store.get_keys
    assert not store.put_keys


def test_exact_directory_reception_keeps_only_one_current_version_per_file():
    store = _Store()
    assert _refresh(store, _inputs()) == "updated"
    before = store.values[LISTING_MASTER_CURRENT_KEY].body
    assert json.loads(before)["US"]["nasdaq_directories"] == [
        {
            "file": "nasdaqlisted.txt",
            "creation_stamp": "1127202608:47",
            "sha256": sha256(_NASDAQ).hexdigest(),
        },
        {
            "file": "otherlisted.txt",
            "creation_stamp": "1127202608:47",
            "sha256": sha256(_NYSE).hexdigest(),
        },
    ]
    repeat = _capture(received_at=_RECEIVED + timedelta(minutes=1))
    # Wrong input ordering does not make the per-file comparison positional.
    assert _refresh(store, replace(repeat, receipts=repeat.receipts[::-1])) == "unchanged"
    assert store.values[LISTING_MASTER_CURRENT_KEY].body == before
    assert store.put_keys == [LISTING_MASTER_CURRENT_KEY]


@pytest.mark.parametrize("lagging", ["nasdaqlisted.txt", "otherlisted.txt"])
def test_later_reception_cannot_hide_one_native_directory_rollback(lagging):
    store = _Store()
    assert _refresh(store, _inputs()) == "updated"
    before = store.values[LISTING_MASTER_CURRENT_KEY].body
    old, new = b"1127202608:45", b"1127202608:48"
    nasdaq = _with_stamp(_NASDAQ, old if lagging == "nasdaqlisted.txt" else new)
    nyse = _with_stamp(_NYSE, old if lagging == "otherlisted.txt" else new)
    receipt = _capture(nasdaq, nyse, received_at=_RECEIVED + timedelta(minutes=1))
    assert _refresh(store, receipt) == "stale"
    assert store.values[LISTING_MASTER_CURRENT_KEY].body == before
    assert store.put_keys == [LISTING_MASTER_CURRENT_KEY]


@pytest.mark.parametrize("with_conflict", [False, True])
def test_native_clock_is_compared_again_after_same_market_conditional_race(with_conflict):
    store = _Store()
    assert _refresh(store, _inputs()) == "updated"
    other_nasdaq = _with_stamp(_NASDAQ, b"1127202608:50")
    challenger_nasdaq = (
        other_nasdaq.replace(b"Fixture", b"Separate fixture")
        if with_conflict
        else _with_stamp(_NASDAQ, b"1127202608:49")
    )
    competing = _capture(
        other_nasdaq,
        _with_stamp(_NYSE, b"1127202608:48"),
        received_at=_RECEIVED + timedelta(minutes=2),
    )
    challenger = _capture(
        challenger_nasdaq,
        _with_stamp(_NYSE, b"1127202608:49"),
        received_at=_RECEIVED + timedelta(minutes=3),
    )

    def later():
        return _CHECKED + timedelta(minutes=4)

    def commit_competing_pair():
        assert _refresh(store, competing, now=later) == "updated"

    store.before_put = commit_competing_pair
    if with_conflict:
        with pytest.raises(ValueError, match="same native creation minute"):
            _refresh(store, challenger, now=later)
    else:
        assert _refresh(store, challenger, now=later) == "stale"
    current = read_current_listing_master(store)["US"]
    assert current.received_at == _RECEIVED + timedelta(minutes=2)
    assert current.nasdaq_directories is not None
    assert current.nasdaq_directories[0].sha256 == sha256(other_nasdaq).hexdigest()
    # Create, competing commit, and single failed conditional attempt. No retry write.
    assert store.put_keys == [LISTING_MASTER_CURRENT_KEY] * 3


def test_same_native_minute_distinct_bytes_are_rejected_even_if_rows_are_identical():
    store = _Store()
    assert _refresh(store, _inputs()) == "updated"
    before = store.values[LISTING_MASTER_CURRENT_KEY].body
    receipt = _capture(
        nyse=_NYSE.replace(b"\n", b"\r\n"),
        received_at=_RECEIVED + timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="same native creation minute"):
        _refresh(store, receipt)
    assert store.values[LISTING_MASTER_CURRENT_KEY].body == before
    assert store.put_keys == [LISTING_MASTER_CURRENT_KEY]


def test_one_unchanged_file_and_one_later_native_minute_can_advance_current():
    store = _Store()
    assert _refresh(store, _inputs()) == "updated"
    nyse = _with_stamp(_NYSE, b"1127202609:00").replace(b"BRK.B", b"BRK.A")
    result = _refresh(
        store,
        _capture(nyse=nyse, received_at=_RECEIVED + timedelta(minutes=13)),
        now=lambda: _CHECKED + timedelta(minutes=14),
    )
    assert result == "updated"
    us = read_current_listing_master(store)["US"]
    assert us.nasdaq_directories is not None
    assert [v.creation_stamp[-5:] for v in us.nasdaq_directories] == ["08:47", "09:00"]
    assert {row.key for row in us.slice.rows if row.listing_state == "listed"} == {
        ("XNAS", "GOOG"),
        ("XNYS", "BRK.A"),
    }
    assert not store.delete_keys


def test_new_verified_listing_notice_can_advance_without_different_directory_bytes():
    store = _Store()
    assert _refresh(store, _inputs()) == "updated"
    notice = VerifiedListingEvent(
        "listed", ("XNAS", "NEXT"), _DAY + timedelta(days=3), "separate verified notice fixture"
    )
    receipt = _capture(received_at=_RECEIVED + timedelta(minutes=1))
    assert _refresh(store, replace(receipt, verified_events=(notice,))) == "updated"
    us = read_current_listing_master(store)["US"]
    assert any(row.key == notice.key and row.listing_state == "scheduled" for row in us.slice.rows)


@pytest.mark.parametrize("initialized", [False, True])
def test_bare_store_call_cannot_drop_verified_us_directory_versions(initialized):
    store = _Store()
    if initialized:
        assert _refresh(store, _inputs()) == "updated"
    before = store.values.copy()
    with pytest.raises(ValueError, match="requires native directory versions"):
        refresh_current_listing_master(
            store,
            market="US",
            session=_DAY,
            source="nasdaq-trader",
            received_at=_RECEIVED + timedelta(minutes=1),
            observations=(ObservedListing(("XNAS", "FAKE"), "Wrong listing", "ordinary"),),
            complete=True,
        )
    assert store.values == before
    assert store.put_keys == ([LISTING_MASTER_CURRENT_KEY] if initialized else [])
