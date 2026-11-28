"""Publish one current Market listing master without re-publishing Machine."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal, Protocol

from pydantic import TypeAdapter

from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    ListingMarket,
    ListingSlice,
    ObservedListing,
    VerifiedListingEvent,
    refresh_listing_slice,
)
from baibai_engine.storage.objects.json import put_private
from baibai_engine.storage.objects.r2 import (
    CheckedObjectMetadataReader,
    LakeCASConflict,
    ObjectStore,
)

LISTING_MASTER_CURRENT_KEY = "market/listing-master/current.json"
# This one object contains both complete name lists, not a small marker.
_MAX_BYTES = 8_000_000

type ListingUpdateStatus = Literal["updated", "unchanged", "stale"]


class ListingMasterStore(ObjectStore, CheckedObjectMetadataReader, Protocol):
    """Use acknowledged conditional PUTs (Boto3R2Store with verify_put_with_head=False)."""


@dataclass(frozen=True, slots=True)
class CurrentListingMarket:
    slice: ListingSlice
    source: str
    received_at: datetime
    received_count: int

    def __post_init__(self) -> None:
        if (
            not self.source.strip()
            or self.received_at.utcoffset() is None
            or self.received_count < 1
        ):
            raise ValueError("invalid current listing master source, receipt or count")
        object.__setattr__(self, "received_at", self.received_at.astimezone(UTC))


_FILE = TypeAdapter(dict[ListingMarket, CurrentListingMarket])


def _read(
    store: CheckedObjectMetadataReader,
) -> tuple[dict[ListingMarket, CurrentListingMarket], str | None]:
    saved = store.get_checked_object(LISTING_MASTER_CURRENT_KEY, max_bytes=_MAX_BYTES)
    if saved is None:
        return {}, None
    body, metadata = saved
    # A stale worker must not discard new fields in the other market.
    current = _FILE.validate_json(body, strict=True, extra="forbid")
    if not current or any(market != entry.slice.market for market, entry in current.items()):
        raise ValueError("current listing master does not match its market sections")
    return current, metadata.etag


def read_current_listing_master(
    store: CheckedObjectMetadataReader,
) -> dict[ListingMarket, CurrentListingMarket]:
    """Read the one current file; a market with no successful update is absent."""
    return _read(store)[0]


def refresh_current_listing_master(
    store: ListingMasterStore,
    *,
    market: ListingMarket,
    session: date,
    source: str,
    received_at: datetime,
    observations: Iterable[ObservedListing],
    complete: bool,
    verified_events: Iterable[VerifiedListingEvent] = (),
) -> ListingUpdateStatus:
    """Replace just one market using a verified complete source observation.

    Reconcile again after one actual CAS conflict: the new prior may include
    identity changes for this market as well as an update to the other market.
    """
    if market not in ("JP", "US") or type(session) is not date or received_at.utcoffset() is None:
        raise ValueError("current listing update requires a market, local session and receipt")
    if complete is not True:
        raise ValueError("incomplete listing capture cannot replace current")
    received_at = received_at.astimezone(UTC)
    rows, events = tuple(observations), tuple(verified_events)
    clock = (session, received_at)
    for last_attempt in (False, True):
        current, etag = _read(store)
        prior = current.get(market)
        prior_clock = (prior.slice.session, prior.received_at) if prior is not None else None
        if prior_clock is not None and clock < prior_clock:
            return "stale"
        refresh = refresh_listing_slice(
            prior.slice if prior is not None else None,
            market=market,
            session=session,
            observations=rows,
            complete=complete,
            verified_events=events,
        )
        replacement = CurrentListingMarket(refresh.slice, source, received_at, len(rows))
        if replacement == prior:
            return "unchanged"
        if clock == prior_clock:
            raise ValueError("different current listings claim the same market receipt")
        current[market] = replacement
        payload = _FILE.dump_json(dict(sorted(current.items())))
        try:
            put_private(
                store,
                LISTING_MASTER_CURRENT_KEY,
                payload,
                previous_etag=etag,
                max_bytes=_MAX_BYTES,
            )
        except LakeCASConflict:
            if last_attempt:
                raise
        else:
            return "updated"
    raise AssertionError("conditional update exited without an outcome")
