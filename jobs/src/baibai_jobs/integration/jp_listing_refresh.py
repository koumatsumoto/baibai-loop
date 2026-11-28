"""Refresh the private JP listing section from a verified native J-Quants session."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime
from typing import Literal

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    FixedCalendarL1,
    verify_calendar_selection,
)
from baibai_engine.data_collection.providers.jquants.contracts import JQuantsRequest
from baibai_engine.data_collection.providers.jquants.master_native import (
    FixedMasterL1,
    read_master_requests,
)
from baibai_engine.machine_analysis.generation.calendar_inputs import calendar_day_session
from baibai_engine.machine_analysis.observations.stocks.jquants_listing import normalize_jp_listings
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    ListingKey,
    VerifiedListingEvent,
)
from baibai_engine.primitives.time import JST
from baibai_jobs.integration.listing_master_current import (
    ListingMasterStore,
    ListingUpdateStatus,
    refresh_current_listing_master,
)

type JPListingRefreshStatus = ListingUpdateStatus | Literal["closed"]


def refresh_current_jp_listings(
    store: ListingMasterStore,
    *,
    session: date,
    evaluated_at: datetime,
    calendar: FixedCalendarL1,
    load_master: Callable[[], FixedMasterL1],
    verified_events: Iterable[VerifiedListingEvent] = (),
) -> JPListingRefreshStatus:
    """Apply only the caller's selected current cycle and only after native verification.

    A confirmed closure skips the master; it never means "use the closest old
    master". This reads existing originals without saving a fixed-input copy.
    """
    if evaluated_at.utcoffset() is None:
        raise ValueError("current JP listing refresh requires an evaluation timezone")
    if session > evaluated_at.astimezone(JST).date():
        raise ValueError("current JP listing session has not started in Japan")
    day = session.isoformat()
    calendar_head = calendar.value()
    scopes = verify_calendar_selection(
        store,
        scopes={day: calendar_head["days"].get(day, {})},
        receipts=calendar_head["receipts"],
        evaluated_at=evaluated_at,
    )
    session_is_open = calendar_day_session(scopes[day])
    if session_is_open is None:
        raise ValueError("native JP calendar cannot prove the current cash-equity session")
    if not session_is_open:
        return "closed"

    request, received_at = _exact_master_request(
        store, load_master(), session=session, evaluated_at=evaluated_at
    )
    capture = normalize_jp_listings(request.pages, session=session)
    events = tuple(verified_events)
    if any(
        not _is_tse_issue_key(event.key)
        or (event.previous_key is not None and not _is_tse_issue_key(event.previous_key))
        for event in events
    ):
        raise ValueError("JP listing notices require an exact TSE issue and class")
    return refresh_current_listing_master(
        store,
        market="JP",
        session=capture.session,
        source="jquants-equities-master",
        received_at=received_at,
        observations=capture.observations,
        complete=True,
        verified_events=events,
    )


def _exact_master_request(
    store: ListingMasterStore,
    fixed: FixedMasterL1,
    *,
    session: date,
    evaluated_at: datetime,
) -> tuple[JQuantsRequest, datetime]:
    day = session.isoformat()
    head = fixed.value()
    scope = head["scopes"].get(day, {})
    adopted = scope.get("adopted")
    if not adopted or scope.get("failure") or scope.get("held"):
        raise ValueError("current JP listing master requires a resolved exact-date receipt")
    for stamp in (scope.get(key) for key in ("confirmed_at", "last_attempt_at", "changed_at")):
        if stamp is not None:
            clock = datetime.fromisoformat(stamp)
            if clock.utcoffset() is None or clock > evaluated_at:
                raise ValueError("native JP master head is later than the evaluation clock")
    retained = tuple(
        read_master_requests(store, scopes={day: scope}, receipts=head["receipts"], days=(session,))
    )
    if len(retained) != 1:
        raise ValueError("native JP master lacks one unambiguous original receipt")
    receipt = retained[0]
    if (
        receipt.request_id != adopted
        or receipt.first_query != {"date": day}
        or receipt.request is None
        or receipt.error is not None
    ):
        raise ValueError("native JP master original differs from the selected exact date")
    # An early publication for this explicit session is valid once the session
    # starts. Use the real latest page receipt, not the confirmation/retry clock.
    received_at = receipt.last_received_at
    if received_at is None or received_at > evaluated_at:
        raise ValueError("native JP master original is later than the evaluation clock")
    return receipt.request, received_at


def _is_tse_issue_key(key: ListingKey) -> bool:
    mic, code = key
    return (
        mic == "XTKS"
        and len(code) == 5
        and code.isascii()
        and code.isalnum()
        and code == code.upper()
    )
