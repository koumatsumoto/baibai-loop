from datetime import date

import pytest

from baibai_engine.data_collection.providers.nasdaq_symbols import read_us_symbol_directory
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    ObservedListing,
    VerifiedListingEvent,
    refresh_listing_slice,
)
from baibai_engine.machine_analysis.observations.stocks.us_listing_input import (
    normalize_us_listings,
)

_NASDAQ = "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
_OTHER = (
    "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
)
_D8, _D9, _D10 = (date(2025, 12, day) for day in (8, 9, 10))
_WMT_NASDAQ = ("WMT", "Walmart Inc. Common Stock", False)
_WMT_NYSE = ("WMT", "Walmart Inc. Common Stock")
_WMT_OLD = ("XNYS", "WMT")
_WMT_NEW = ("XNAS", "WMT")
_MOVE = VerifiedListingEvent(
    "moved", _WMT_NEW, _D9, "Nasdaq Trader DTN2025-35", previous_key=_WMT_OLD
)


def _native(*, nasdaq=(), nyse=()):
    xn = (("NVDA", "NVIDIA Corporation Common Stock", False), *nasdaq)
    yn = (
        ("BRK.A", "Berkshire Hathaway Inc."),
        ("BRK.B", "Berkshire Hathaway Inc. New Common Stock"),
        ("LEN.B", "Lennar Corporation Class B"),
        *nyse,
    )
    n = _NASDAQ + "".join(
        f"{ticker}|{name}|Q|N|N|100|{'Y' if fund else 'N'}|N\n" for ticker, name, fund in xn
    )
    y = _OTHER + "".join(f"{ticker}|{name}|N|{ticker}|N|100|N|{ticker}\n" for ticker, name in yn)
    return (
        read_us_symbol_directory(
            (n + "File Creation Time: 1209202508:41|||||||\n").encode(), file="nasdaqlisted.txt"
        ),
        read_us_symbol_directory(
            (y + "File Creation Time: 1209202508:39||||||\n").encode(), file="otherlisted.txt"
        ),
    )


def _refresh(previous=None, *, day=_D8, native=None, events=(), complete=True):
    return refresh_listing_slice(
        previous,
        market="US",
        session=day,
        observations=normalize_us_listings(
            _native() if native is None else native, complete=complete
        ),
        complete=complete,
        verified_events=events,
    )


def _row(refresh, key):
    return next(row for row in refresh.slice.rows if row.key == key)


def test_baseline_keeps_primary_classes_separate_and_does_not_invent_effective_dates():
    state = _refresh()
    cls = [_row(state, ("XNYS", ticker)) for ticker in ("BRK.A", "BRK.B", "LEN.B")]
    assert len({row.instrument_id for row in cls}) == 3
    assert all(row.listing_state == "listed" and row.effective_session is None for row in cls)
    assert _refresh(state.slice).changes == ()


def test_future_new_listing_even_if_native_preappears_cannot_activate_early():
    key = ("XNAS", "NEW")
    notice = VerifiedListingEvent("listed", key, _D9, "official IPO notice")
    native = _native(nasdaq=(("NEW", "New Inc. Common Stock", False),))
    planned = _refresh(native=native, events=(notice,))
    row = _row(planned, key)
    assert row.listing_state == "scheduled"
    assert row.instrument_id is None
    assert row.effective_session == _D9
    assert _refresh(planned.slice, native=native, events=(notice,)).changes == ()
    launched = _refresh(planned.slice, day=_D9, native=native, events=(notice,))
    assert _row(launched, key).listing_state == "listed"
    assert _row(launched, key).effective_session == _D9
    assert _refresh(launched.slice, day=_D10, native=native).changes == ()


def test_wmt_verified_effective_move_keeps_identity_without_a_false_delist():
    before = _refresh(native=_native(nyse=(_WMT_NYSE,)), events=(_MOVE,))
    old = _row(before, _WMT_OLD)
    assert old.listing_state == "listed"
    assert _row(before, _WMT_NEW).listing_state == "scheduled"
    new_native = _native(nasdaq=(_WMT_NASDAQ,))
    moved = _refresh(before.slice, day=_D9, native=new_native, events=(_MOVE,))
    assert _row(moved, _WMT_NEW).instrument_id == old.instrument_id
    assert [c.kind for c in moved.changes] == ["moved"]
    assert all(row.key != _WMT_OLD for row in moved.slice.rows)
    assert _refresh(moved.slice, day=_D9, native=new_native, events=(_MOVE,)).changes == ()


def test_wmt_unproved_transfer_does_not_join_two_price_identities():
    before = _refresh(native=_native(nyse=(_WMT_NYSE,)))
    after = _refresh(before.slice, day=_D9, native=_native(nasdaq=(_WMT_NASDAQ,)))
    assert _row(after, _WMT_OLD).listing_state == "unverified"
    assert _row(after, _WMT_NEW).instrument_id != _row(before, _WMT_OLD).instrument_id
    assert {c.kind for c in after.changes} == {"listed", "unverified"}


def test_wmt_incoherent_overlap_holds_only_wmt_and_can_recover_once():
    before = _refresh(native=_native(nyse=(_WMT_NYSE,)))
    overlap = _refresh(
        before.slice,
        day=_D9,
        native=_native(nasdaq=(_WMT_NASDAQ,), nyse=(_WMT_NYSE,)),
        events=(_MOVE,),
    )
    assert (
        _row(overlap, _WMT_OLD).listing_state
        == _row(overlap, _WMT_NEW).listing_state
        == "unverified"
    )
    assert _row(overlap, ("XNAS", "NVDA")).listing_state == "listed"
    recovered = _refresh(
        overlap.slice, day=_D9, native=_native(nasdaq=(_WMT_NASDAQ,)), events=(_MOVE,)
    )
    assert _row(recovered, _WMT_NEW).instrument_id == _row(before, _WMT_OLD).instrument_id
    assert [c.kind for c in recovered.changes] == ["moved"]


def test_disappearance_is_not_delist_but_verified_effective_absent_is():
    before = _refresh(native=_native(nyse=(_WMT_NYSE,)))
    pending = VerifiedListingEvent("delisted", _WMT_OLD, _D10, "exchange effective notice")
    not_yet = _refresh(before.slice, day=_D9, events=(pending,))
    assert _row(not_yet, _WMT_OLD).listing_state == "unverified"
    assert [c.kind for c in not_yet.changes] == ["unverified"]
    official = _refresh(not_yet.slice, day=_D10, events=(pending,))
    assert [c.kind for c in official.changes] == ["delisted"]
    assert official.changes[0].official_event == pending
    assert all(row.key != _WMT_OLD for row in official.slice.rows)
    assert _refresh(official.slice, day=_D10, events=(pending,)).changes == ()


def test_effective_delist_contradicting_native_presence_must_not_discard_the_row():
    native = _native(nyse=(_WMT_NYSE,))
    old = _refresh(native=native)
    notice = VerifiedListingEvent("delisted", _WMT_OLD, _D9, "exchange effective notice")
    conflict = _refresh(old.slice, day=_D9, native=native, events=(notice,))
    assert _row(conflict, _WMT_OLD).listing_state == "unverified"
    assert [c.kind for c in conflict.changes] == ["unverified"]


def test_reused_meta_symbol_does_not_splice_after_an_unproved_gap_or_type_change():
    key = ("XNAS", "META")
    old_native = _native(nasdaq=(("META", "Roundhill Ball Metaverse ETF", True),))
    new_native = _native(nasdaq=(("META", "Meta Platforms Class A Common Stock", False),))
    old = _refresh(native=old_native)
    missing = _refresh(old.slice, day=_D9)
    current = _refresh(missing.slice, day=_D10, native=new_native)
    assert _row(current, key).instrument_id != _row(old, key).instrument_id
    without_gap = _refresh(old.slice, day=_D9, native=new_native)
    assert _row(without_gap, key).instrument_id != _row(old, key).instrument_id


def test_verified_reuse_on_same_session_gets_new_identity_and_is_idempotent():
    old = _refresh(native=_native(nasdaq=(_WMT_NASDAQ,)))
    notice = VerifiedListingEvent("reused", _WMT_NEW, _D8, "official different issue")
    second = _refresh(old.slice, native=_native(nasdaq=(_WMT_NASDAQ,)), events=(notice,))
    assert [c.kind for c in second.changes] == ["identity_changed"]
    assert _row(second, _WMT_NEW).instrument_id != _row(old, _WMT_NEW).instrument_id
    assert (
        _refresh(second.slice, native=_native(nasdaq=(_WMT_NASDAQ,)), events=(notice,)).changes
        == ()
    )


def test_delist_notice_before_replacement_does_not_block_the_new_issue():
    newer = _refresh(day=_D10, native=_native(nasdaq=(_WMT_NASDAQ,)))
    historical = VerifiedListingEvent("delisted", _WMT_NEW, _D8, "retired original issue")
    current = _refresh(
        newer.slice, day=_D10, native=_native(nasdaq=(_WMT_NASDAQ,)), events=(historical,)
    )
    assert current.changes == ()
    assert _row(current, _WMT_NEW).listing_state == "listed"


def test_conflicting_effective_notices_hold_only_affected_rows():
    previous = _refresh(native=_native(nyse=(_WMT_NYSE,)))
    other = VerifiedListingEvent("delisted", _WMT_OLD, _D9, "contradictory current notice")
    current = _refresh(
        previous.slice, day=_D9, native=_native(nasdaq=(_WMT_NASDAQ,)), events=(_MOVE, other)
    )
    assert [c.kind for c in current.changes] == ["unverified", "unverified"]
    assert _row(current, ("XNYS", "LEN.B")).listing_state == "listed"


def test_older_partial_missing_or_duplicate_input_cannot_replace_the_current_view():
    previous = _refresh(day=_D9).slice
    with pytest.raises(ValueError, match="older listing session"):
        _refresh(previous)
    good = _native()
    for directories, complete in [(good, False), (good[:1], True), ((good[0], good[0]), True)]:
        with pytest.raises(ValueError, match="both complete"):
            _refresh(previous, day=_D10, native=directories, complete=complete)


def test_exchange_preannounces_move_without_making_old_listing_unverified_early():
    # Two native files can mention the same symbol before a dated transfer takes effect.
    early = _refresh(native=_native(nasdaq=(_WMT_NASDAQ,), nyse=(_WMT_NYSE,)), events=(_MOVE,))
    assert _row(early, _WMT_OLD).listing_state == "listed"
    assert _row(early, _WMT_NEW).listing_state == "scheduled"


def test_dated_new_listing_without_a_native_row_is_unverified_not_active():
    key = ("XNAS", "NEW")
    notice = VerifiedListingEvent("listed", key, _D9, "official IPO notice")
    state = _refresh(day=_D9, events=(notice,))
    assert _row(state, key).listing_state == "unverified"
    assert _row(state, key).instrument_id is None
    assert _row(state, key).effective_session == _D9
    assert _refresh(state.slice, day=_D9, events=(notice,)).changes == ()


def test_effective_move_with_old_only_holds_both_symbols_until_corroborated():
    old_only = _native(nyse=(_WMT_NYSE,))
    before = _refresh(native=old_only)
    unresolved = _refresh(before.slice, day=_D9, native=old_only, events=(_MOVE,))
    assert _row(unresolved, _WMT_OLD).listing_state == "unverified"
    assert _row(unresolved, _WMT_NEW).listing_state == "unverified"
    assert _row(unresolved, _WMT_NEW).instrument_id is None


def test_unproved_same_day_reappearance_never_cycles_to_the_first_identity():
    native = _native(nasdaq=(_WMT_NASDAQ,))
    initial = _refresh(native=native)
    missing = _refresh(initial.slice)
    next_identity = _refresh(missing.slice, native=native)
    missing_again = _refresh(next_identity.slice)
    third = _refresh(missing_again.slice, native=native)
    assert (
        len({_row(state, _WMT_NEW).instrument_id for state in (initial, next_identity, third)}) == 3
    )


def test_effective_move_cannot_retire_a_future_issue_using_the_old_key_on_rerun():
    future = VerifiedListingEvent("listed", _WMT_OLD, _D10, "planned separate use of ticker")
    native = _native(nasdaq=(_WMT_NASDAQ,))
    first = _refresh(day=_D9, native=native, events=(_MOVE, future))
    assert _row(first, _WMT_NEW).listing_state == "listed"
    assert _row(first, _WMT_OLD).listing_state == "scheduled"
    assert _refresh(first.slice, day=_D9, native=native, events=(_MOVE, future)).changes == ()


def test_past_delisting_and_tomorrows_new_issue_same_key_do_not_conflict_on_replay():
    retire = VerifiedListingEvent("delisted", _WMT_NEW, _D8, "previous issue")
    future = VerifiedListingEvent("listed", _WMT_NEW, _D10, "different new issue")
    native = _native(nasdaq=(_WMT_NASDAQ,))
    first = _refresh(day=_D9, native=native, events=(retire, future))
    assert _row(first, _WMT_NEW).listing_state == "scheduled"
    assert _refresh(first.slice, day=_D9, native=native, events=(retire, future)).changes == ()


def test_delist_prior_actual_issue_but_retain_schedule_of_different_issue_on_same_key():
    old = _refresh(native=_native(nasdaq=(_WMT_NASDAQ,)))
    retire = VerifiedListingEvent("delisted", _WMT_NEW, _D9, "previous issue")
    future = VerifiedListingEvent("listed", _WMT_NEW, _D10, "different new issue")
    next_state = _refresh(old.slice, day=_D9, events=(retire, future))
    assert [c.kind for c in next_state.changes] == ["delisted", "scheduled"]
    assert _row(next_state, _WMT_NEW).listing_state == "scheduled"
    assert _refresh(next_state.slice, day=_D9, events=(retire, future)).changes == ()


def test_current_effective_notice_and_seen_listing_correct_a_previous_later_schedule():
    key = ("XNAS", "NEW")
    earlier_announcement = VerifiedListingEvent("listed", key, _D10, "earlier planned date")
    previous = _refresh(events=(earlier_announcement,))
    revised = VerifiedListingEvent("listed", key, _D9, "revised effective date")
    native = _native(nasdaq=(("NEW", "New Inc.", False),))
    current = _refresh(previous.slice, day=_D9, native=native, events=(revised,))
    assert _row(current, key).listing_state == "listed"
    assert _row(current, key).effective_session == _D9
    assert _refresh(current.slice, day=_D9, native=native, events=(revised,)).changes == ()


def test_stale_move_no_longer_reauthorizes_new_destination_identity_or_rewinds_its_date():
    old = _refresh(native=_native(nyse=(_WMT_NYSE,)))
    moved = _refresh(old.slice, day=_D9, native=_native(nasdaq=(_WMT_NASDAQ,)), events=(_MOVE,))
    different = _native(nasdaq=(("WMT", "Another Inc.", False),))
    no_proof = _refresh(moved.slice, day=_D10, native=different, events=(_MOVE,))
    assert _row(no_proof, _WMT_NEW).instrument_id != _row(moved, _WMT_NEW).instrument_id
    assert _row(no_proof, _WMT_NEW).effective_session is None
    reuse = VerifiedListingEvent("reused", _WMT_NEW, _D10, "same symbol, different issue")
    confirmed = _refresh(moved.slice, day=_D10, native=different, events=(reuse,))
    current = _refresh(confirmed.slice, day=date(2025, 12, 11), native=different, events=(_MOVE,))
    assert current.changes == ()
    assert _row(current, _WMT_NEW).effective_session == _D10


@pytest.mark.parametrize("event_kind", ["listed", "reused"])
def test_different_issue_appears_before_verified_effective_date_on_an_incumbent_key(event_kind):
    old = _refresh(native=_native(nasdaq=(_WMT_NASDAQ,)))
    pending = VerifiedListingEvent(event_kind, _WMT_NEW, _D10, "planned different issue")
    different = _native(nasdaq=(("WMT", "Another Inc.", False),))
    early = _refresh(old.slice, day=_D9, native=different, events=(pending,))
    assert _row(early, _WMT_NEW).listing_state == "unverified"
    assert _refresh(early.slice, day=_D9, native=different, events=(pending,)).changes == ()
    effective = _refresh(early.slice, day=_D10, native=different, events=(pending,))
    assert _row(effective, _WMT_NEW).listing_state == "listed"
    assert _row(effective, _WMT_NEW).instrument_id != _row(old, _WMT_NEW).instrument_id


def test_same_processor_handles_jquants_observation_and_jpx_effective_delist_separate_from_us():
    retired = ("XTKS", "10010")
    active = ("XTKS", "10020")
    before = refresh_listing_slice(
        None,
        market="JP",
        session=_D8,
        observations=(
            ObservedListing(retired, "先行株式会社", None),
            ObservedListing(active, "営業株式会社", None),
        ),
        complete=True,
    )
    official = VerifiedListingEvent("delisted", retired, _D9, "JPX official effective date")
    after = refresh_listing_slice(
        before.slice,
        market="JP",
        session=_D9,
        observations=(ObservedListing(active, "営業株式会社", None),),
        complete=True,
        verified_events=(official,),
    )
    assert after.slice.market == "JP"
    assert [row.key for row in after.slice.rows] == [active]
    assert [c.kind for c in after.changes] == ["delisted"]
    assert after.changes[0].instrument_id.startswith("jp:XTKS:10010:")
    with pytest.raises(ValueError, match="other-market"):
        _refresh(before.slice)


def test_reordered_equal_current_evidence_does_not_reinterpret_its_own_unresolved_history():
    conflicting = VerifiedListingEvent("delisted", _WMT_NEW, _D8, "prior independent notice")
    native = _native(nasdaq=(_WMT_NASDAQ,))
    first = _refresh(day=_D9, native=native, events=(_MOVE, conflicting))
    assert _row(first, _WMT_NEW).listing_state == "unverified"
    assert first.slice.input_fingerprint is not None
    rerun = _refresh(
        first.slice, day=_D9, native=native[::-1], events=(conflicting, _MOVE, conflicting)
    )
    assert rerun.slice == first.slice
    assert not rerun.changes
    corrected = _refresh(first.slice, day=_D9, native=native, events=(_MOVE,))
    assert _row(corrected, _WMT_NEW).listing_state == "listed"
    assert corrected.slice.input_fingerprint != first.slice.input_fingerprint
