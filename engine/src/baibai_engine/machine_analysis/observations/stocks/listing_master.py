"""Shared reconciliation for one current listing-master market slice."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import date
from hashlib import sha256
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

type ListingMarket = Literal["JP", "US"]
type ListingKey = tuple[str, str]  # Primary listing MIC and exact venue-local symbol.
type ListingState = Literal["listed", "scheduled", "unverified"]
type ListingEventKind = Literal["listed", "delisted", "moved", "reused"]
type ListingChangeKind = Literal[
    "listed", "scheduled", "unverified", "delisted", "moved", "identity_changed"
]


@dataclass(frozen=True, slots=True)
class JapanListingAttributes:
    """J-Quants product, market segment and Sector33 on this precise issue line."""

    product_category: str
    market_segment: str
    sector_33: str
    market_segment_code: str | None = None
    sector_33_code: str | None = None


@dataclass(frozen=True, slots=True)
class ObservedListing:
    key: ListingKey
    security_name: str
    issue_type: str | None
    japan: JapanListingAttributes | None = None


@dataclass(frozen=True, slots=True)
class VerifiedListingEvent:
    """A scoped official notice, verified by the caller, not by this reducer.

    A move requires proof of the same security and class and names the old key.
    A reuse requires proof that the same key now refers to a different issue.
    """

    kind: ListingEventKind
    key: ListingKey
    effective_session: date
    evidence: str
    previous_key: ListingKey | None = None


@dataclass(frozen=True, slots=True)
class ListingMasterRow:
    key: ListingKey
    security_name: str | None
    issue_type: str | None
    listing_state: ListingState
    instrument_id: str | None = None
    identity_since: date | None = None  # First assigned identity, not necessarily the IPO date.
    effective_session: date | None = None  # Only a verified official event supplies this date.
    japan: JapanListingAttributes | None = None


@dataclass(frozen=True, slots=True)
class ListingSlice:
    market: ListingMarket
    session: date
    rows: tuple[ListingMasterRow, ...]
    input_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class ListingChange:
    kind: ListingChangeKind
    key: ListingKey
    instrument_id: str | None
    official_event: VerifiedListingEvent | None = None


@dataclass(frozen=True, slots=True)
class ListingRefresh:
    """Only the slice is current state; changes are a transient run result."""

    slice: ListingSlice
    changes: tuple[ListingChange, ...]


def _current_listings(
    observations: Iterable[ObservedListing], *, complete: bool
) -> dict[ListingKey, ObservedListing]:
    rows = tuple(observations)
    result = {row.key: row for row in rows}
    if complete is not True or not rows or len(result) != len(rows):
        raise ValueError("current listing slice requires complete, unique observations")
    return result


def _same_observed_issue(
    previous: ListingMasterRow | None, observation: ObservedListing | None
) -> bool:
    return (
        previous is not None
        and previous.listing_state == "listed"
        and observation is not None
        and previous.security_name == observation.security_name
        and previous.issue_type == observation.issue_type
    )


def _input_fingerprint(
    observations: dict[ListingKey, ObservedListing],
    events: tuple[VerifiedListingEvent, ...],
) -> str:
    # Keep only the current fingerprint; replaying the same inputs cannot reinterpret
    # an unresolved notice against the state that notice itself produced.
    canonical = (
        [
            (key, native.security_name, native.issue_type, asdict(native.japan))
            if native.japan is not None
            else (key, native.security_name, native.issue_type)
            for key, native in sorted(observations.items())
        ],
        sorted(
            {
                (
                    event.kind,
                    event.key,
                    event.effective_session.isoformat(),
                    event.evidence,
                    event.previous_key or (),
                )
                for event in events
            }
        ),
    )
    return sha256(
        json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _events_by_key(
    events: Iterable[VerifiedListingEvent],
    session: date,
    previous: dict[ListingKey, ListingMasterRow],
) -> tuple[
    dict[ListingKey, VerifiedListingEvent],
    dict[ListingKey, VerifiedListingEvent],
    set[ListingKey],
]:
    active: dict[ListingKey, VerifiedListingEvent] = {}
    pending: dict[ListingKey, VerifiedListingEvent] = {}
    blocked: set[ListingKey] = set()
    touched: dict[ListingKey, list[VerifiedListingEvent]] = defaultdict(list)
    seen: set[tuple[ListingEventKind, ListingKey, date, ListingKey | None]] = set()
    for event in sorted(events, key=lambda item: (item.effective_session, item.evidence)):
        if not event.evidence.strip() or (event.kind == "moved") != (
            event.previous_key is not None
        ):
            raise ValueError("invalid verified listing event")
        if event.key == event.previous_key:
            raise ValueError("listing move must change the primary listing key")
        signature = (event.kind, event.key, event.effective_session, event.previous_key)
        if signature in seen:
            continue
        seen.add(signature)
        if event.effective_session > session:
            if event.kind in ("listed", "moved", "reused"):
                pending.setdefault(event.key, event)
            continue
        old = previous.get(event.previous_key or event.key)
        target = previous.get(event.key)
        if old is not None:
            later_unissued = (
                (event.kind == "delisted" or event.previous_key is not None)
                and old.instrument_id is None
                and old.effective_session is not None
                and old.effective_session > event.effective_session
            )
            if later_unissued or (
                old.identity_since is not None and old.identity_since > event.effective_session
            ):
                continue  # The source key is now a later issued or scheduled instance.
        if (
            event.kind == "moved"
            and (old is None or old.instrument_id is None)
            and target is not None
            and target.instrument_id is not None
        ):
            # An old notice cannot re-authorize a destination now tracked independently.
            continue
        active[event.key] = event
        touched[event.key].append(event)
        if event.previous_key is not None:
            touched[event.previous_key].append(event)
    for contenders in touched.values():
        if len(contenders) > 1:
            for event in contenders:
                blocked.add(event.key)
                if event.previous_key is not None:
                    blocked.add(event.previous_key)
    return active, pending, blocked


def _listed_row(
    key: ListingKey,
    native: ObservedListing,
    previous: ListingMasterRow | None,
    event: VerifiedListingEvent | None,
    session: date,
    *,
    market: ListingMarket,
    verified_continuity: bool,
) -> ListingMasterRow:
    identity_since = session
    prefix = f"{market.lower()}:{key[0]}:{key[1]}"
    new_id = f"{prefix}:{session.isoformat()}"
    effective = event.effective_session if event is not None else None
    if event is not None and event.kind == "reused":
        identity_since = event.effective_session
        new_id = f"{prefix}:{identity_since.isoformat()}:reuse"
    elif (
        previous is not None
        and previous.instrument_id is not None
        and (_same_observed_issue(previous, native) or verified_continuity)
    ):
        identity_since = previous.identity_since or session
        new_id = previous.instrument_id
        effective = effective or previous.effective_session
    elif previous is not None and previous.instrument_id is not None:
        # Same-day corrections cannot cycle back to an earlier identity of this key.
        new_id += ":" + uuid5(NAMESPACE_URL, previous.instrument_id).hex
    return ListingMasterRow(
        key,
        native.security_name,
        native.issue_type,
        "listed",
        new_id,
        identity_since,
        effective,
        japan=native.japan,
    )


def refresh_listing_slice(
    previous: ListingSlice | None,
    *,
    market: ListingMarket,
    session: date,
    observations: Iterable[ObservedListing],
    complete: bool,
    verified_events: Iterable[VerifiedListingEvent] = (),
) -> ListingRefresh:
    """Use an already verified, coherent capture for one local-market session.

    A partial or older capture cannot replace current state. Omission from a
    complete list is unverified, never official delisting. Same-symbol venue
    overlaps and conflicting notices block only the affected listings. A trusted
    official notice can join a move, not a price bar or a broker ticker.
    """
    if previous is not None and (previous.market != market or session < previous.session):
        raise ValueError("other-market or older listing session cannot replace this slice")
    native = _current_listings(observations, complete=complete)
    if market != "JP" and any(listing.japan is not None for listing in native.values()):
        raise ValueError("Japan listing attributes require a JP slice")
    events = tuple(verified_events)
    fingerprint = _input_fingerprint(native, events)
    if (
        previous is not None
        and previous.session == session
        and previous.input_fingerprint == fingerprint
    ):
        return ListingRefresh(previous, ())
    prior = {row.key: row for row in previous.rows} if previous is not None else {}
    active, pending, blocked = _events_by_key(events, session, prior)
    positive_events = {key for key, event in active.items() if event.kind != "delisted"}
    future_sessions = {key: event.effective_session for key, event in pending.items()}
    for key, prior_row in prior.items():
        if (
            prior_row.listing_state == "scheduled"
            and prior_row.effective_session is not None
            and prior_row.effective_session > session
        ):
            future_sessions.setdefault(key, prior_row.effective_session)
    unlaunched = {
        key
        for key in future_sessions
        if key not in positive_events and not _same_observed_issue(prior.get(key), native.get(key))
    }
    observed_active = set(native) - unlaunched
    symbols: dict[str, list[ListingKey]] = defaultdict(list)
    for key in observed_active:
        symbols[key[1]].append(key)
    for matches in symbols.values():
        if len(matches) > 1:
            blocked.update(matches)

    retired: dict[ListingKey, VerifiedListingEvent] = {}
    moved: dict[ListingKey, ListingMasterRow] = {}
    for key, notice in active.items():
        if notice.kind == "delisted":
            previous_issue_exists = key in prior and prior[key].instrument_id is not None
            if key in observed_active:
                # A historical notice cannot suppress an independently observed later listing.
                if previous_issue_exists or notice.effective_session == session:
                    blocked.add(key)
            elif key not in blocked and previous_issue_exists:
                retired[key] = notice
        old_key = notice.previous_key
        if old_key is None:
            continue
        if old_key in blocked or key in blocked or old_key in observed_active or key not in native:
            blocked.update((old_key, key))
        elif old_key in prior and prior[old_key].instrument_id is not None:
            retired[old_key] = notice
            moved[key] = prior[old_key]

    current: dict[ListingKey, ListingMasterRow] = {}
    for key in sorted(set(prior) | set(native) | set(future_sessions) | positive_events):
        former, listing, event = prior.get(key), native.get(key), active.get(key)
        scheduled_for = future_sessions.get(key)
        if (
            key not in blocked
            and scheduled_for is not None
            and (
                key in retired
                or (key in unlaunched and (former is None or former.instrument_id is None))
            )
        ):
            current[key] = ListingMasterRow(
                key, None, None, "scheduled", effective_session=scheduled_for
            )
        elif key in retired:
            continue
        elif key not in blocked and key not in unlaunched and listing is not None:
            if event is not None and event.kind == "delisted":
                event = None  # This is a newer independently observed instance of the key.
            current[key] = _listed_row(
                key,
                listing,
                moved.get(key, former),
                event,
                session,
                market=market,
                verified_continuity=key in moved,
            )
        elif former is not None:
            current[key] = replace(former, listing_state="unverified")
        else:
            current[key] = ListingMasterRow(
                key,
                listing.security_name if listing is not None else None,
                listing.issue_type if listing is not None else None,
                "unverified",
                effective_session=event.effective_session
                if key in positive_events and event
                else None,
                japan=listing.japan if listing is not None else None,
            )

    changes: list[ListingChange] = []
    for key in sorted(set(prior) | set(current)):
        former, row = prior.get(key), current.get(key)
        event = active.get(key)
        if former == row or (
            former is not None and row is not None and replace(former, japan=row.japan) == row
        ):
            continue  # A sector/segment correction is not a listing event.
        record = retired.get(key)
        if record is not None and record.kind == "delisted" and former is not None:
            changes.append(ListingChange("delisted", key, former.instrument_id, record))
        if row is None:
            continue
        if row.listing_state == "scheduled":
            changes.append(ListingChange("scheduled", key, None, pending.get(key)))
        elif row.listing_state == "unverified":
            changes.append(ListingChange("unverified", key, row.instrument_id))
        elif key in moved and moved[key].instrument_id == row.instrument_id:
            changes.append(ListingChange("moved", key, row.instrument_id, event))
        elif former is not None and former.instrument_id not in (None, row.instrument_id):
            changes.append(ListingChange("identity_changed", key, row.instrument_id, event))
        else:
            changes.append(ListingChange("listed", key, row.instrument_id, event))
    return ListingRefresh(
        ListingSlice(market, session, tuple(current.values()), fingerprint), tuple(changes)
    )
