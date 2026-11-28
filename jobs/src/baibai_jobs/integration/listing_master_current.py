"""Publish one current Market listing master without re-publishing Machine."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Annotated, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import Field, TypeAdapter

from baibai_engine.data_collection.providers.nasdaq_symbols import (
    DirectoryFile,
    directory_creation_minute,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    ListingKey,
    ListingMarket,
    ListingSlice,
    ObservedListing,
    VerifiedListingEvent,
    refresh_listing_slice,
)
from baibai_engine.machine_analysis.observations.stocks.us_universe import (
    RakutenOrdinaryRow,
    RakutenOrdinarySnapshot,
    RakutenRestriction,
    RakutenRestrictionRow,
    rakuten_listing_key,
)
from baibai_engine.storage.objects.json import put_private
from baibai_engine.storage.objects.r2 import (
    CheckedObjectMetadataReader,
    LakeCASConflict,
    ObjectStore,
)

NASDAQ_DIRECTORY_SOURCE = "nasdaq-trader"
LISTING_MASTER_CURRENT_KEY = "market/listing-master/current.json"
# This one object contains both complete name lists, not a small marker.
_MAX_BYTES = 8_000_000
_RAKUTEN_NY = ZoneInfo("America/New_York")

type ListingUpdateStatus = Literal["updated", "unchanged", "stale"]


type RakutenBrokerState = Literal["catalog_target"] | RakutenRestriction


@dataclass(frozen=True, slots=True)
class RakutenListingBinding:
    """A verified broker join to the exact current exchange instrument."""

    listing_key: ListingKey
    instrument_id: str
    observation: RakutenOrdinaryRow | RakutenRestrictionRow


@dataclass(frozen=True, slots=True)
class RakutenBrokerRow:
    listing_key: ListingKey
    instrument_id: str
    broker_symbol: str
    state: RakutenBrokerState


@dataclass(frozen=True, slots=True)
class CurrentRakutenScope:
    """Current broker facts only. Use the date/cutoff-aware read, not the raw attachment."""

    effective_session: date
    catalog_received_at: datetime
    exclusions_received_at: datetime
    catalog_count: int
    exclusions_count: int
    rows: tuple[RakutenBrokerRow, ...]

    def __post_init__(self) -> None:
        if (
            self.catalog_count < 1
            or self.exclusions_count < 0
            or any(
                clock.utcoffset() is None
                or clock.astimezone(_RAKUTEN_NY).date() != self.effective_session
                for clock in self.clocks
            )
            or len({row.listing_key for row in self.rows}) != len(self.rows)
        ):
            raise ValueError("invalid current Rakuten scope")
        object.__setattr__(self, "catalog_received_at", self.catalog_received_at.astimezone(UTC))
        object.__setattr__(
            self, "exclusions_received_at", self.exclusions_received_at.astimezone(UTC)
        )

    @property
    def clocks(self) -> tuple[datetime, datetime]:
        return self.catalog_received_at, self.exclusions_received_at


_RAKUTEN_STATES: dict[str, RakutenBrokerState] = {
    "buyable": "catalog_target",
    "sell_only": "sell_only",
    "buy_suspended": "buy_suspended",
}


class ListingMasterStore(ObjectStore, CheckedObjectMetadataReader, Protocol):
    """Use acknowledged conditional PUTs (Boto3R2Store with verify_put_with_head=False)."""


@dataclass(frozen=True, slots=True)
class NasdaqDirectoryVersion:
    """Current provider wall minute plus exact received bytes identity, per file."""

    file: DirectoryFile
    creation_stamp: str
    sha256: str

    @property
    def native_minute(self) -> datetime:
        return directory_creation_minute(self.creation_stamp)


def _checked_directory_versions(
    versions: tuple[NasdaqDirectoryVersion, ...] | None,
    *,
    market: ListingMarket,
    source: str,
    session: date,
) -> tuple[NasdaqDirectoryVersion, ...] | None:
    if market != "US":
        if versions is not None:
            raise ValueError("Nasdaq directory versions require the US market")
        return None
    if (
        source != NASDAQ_DIRECTORY_SOURCE
        or versions is None
        or len(versions) != 2
        or {version.file for version in versions} != {"nasdaqlisted.txt", "otherlisted.txt"}
    ):
        raise ValueError("US listing master requires native directory versions for both files")
    if any(version.native_minute.date() != session for version in versions):
        raise ValueError("US listing directory native creation date does not match the session")
    if any(re.fullmatch(r"[0-9a-f]{64}", version.sha256) is None for version in versions):
        raise ValueError("invalid Nasdaq directory SHA-256")
    return tuple(sorted(versions, key=lambda version: version.file))


def _is_directory_rollback(
    previous: tuple[NasdaqDirectoryVersion, ...] | None,
    received: tuple[NasdaqDirectoryVersion, ...] | None,
) -> bool:
    if previous is None or received is None:
        return False
    rollback = False
    for old, new in zip(previous, received, strict=True):
        old_minute, new_minute = old.native_minute, new.native_minute
        if new_minute == old_minute and new.sha256 != old.sha256:
            raise ValueError(f"{new.file}: different bytes claim the same native creation minute")
        rollback |= new_minute < old_minute
    return rollback


@dataclass(frozen=True, slots=True)
class CurrentListingMarket:
    slice: ListingSlice
    source: str
    received_at: datetime
    received_count: int
    nasdaq_directories: Annotated[
        tuple[NasdaqDirectoryVersion, ...] | None, Field(exclude_if=lambda value: value is None)
    ] = None
    rakuten: Annotated[
        CurrentRakutenScope | None, Field(exclude_if=lambda scope: scope is None)
    ] = None

    def __post_init__(self) -> None:
        if (
            not self.source.strip()
            or self.received_at.utcoffset() is None
            or self.received_count < 1
        ):
            raise ValueError("invalid current listing master source, receipt or count")
        if self.rakuten is not None and self.slice.market != "US":
            raise ValueError("Rakuten scope belongs to US listings only")
        object.__setattr__(self, "received_at", self.received_at.astimezone(UTC))
        object.__setattr__(
            self,
            "nasdaq_directories",
            _checked_directory_versions(
                self.nasdaq_directories,
                market=self.slice.market,
                source=self.source,
                session=self.slice.session,
            ),
        )


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
    nasdaq_directories: tuple[NasdaqDirectoryVersion, ...] | None = None,
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
    versions = _checked_directory_versions(
        nasdaq_directories, market=market, source=source, session=session
    )
    rows, events = tuple(observations), tuple(verified_events)
    clock = (session, received_at)

    def prepare(prior: CurrentListingMarket | None) -> CurrentListingMarket | None:
        prior_clock = (prior.slice.session, prior.received_at) if prior is not None else None
        if prior_clock is not None and clock < prior_clock:
            return None
        if (
            prior is not None
            and session == prior.slice.session
            and _is_directory_rollback(prior.nasdaq_directories, versions)
        ):
            return None

        refresh = refresh_listing_slice(
            prior.slice if prior is not None else None,
            market=market,
            session=session,
            observations=rows,
            complete=complete,
            verified_events=events,
        )
        if prior is None:
            return CurrentListingMarket(refresh.slice, source, received_at, len(rows), versions)
        if (
            versions is not None
            and versions == prior.nasdaq_directories
            and refresh.slice == prior.slice
        ):
            return prior  # A transport retry of the same files/evidence is not a new capture.
        replacement = replace(
            prior,
            slice=refresh.slice,
            source=source,
            received_at=received_at,
            received_count=len(rows),
            nasdaq_directories=versions,
        )
        if replacement != prior and clock == prior_clock:
            raise ValueError("different current listings claim the same market receipt")
        return replacement

    return _update_market(store, market, prepare)


def _update_market(
    store: ListingMasterStore,
    market: ListingMarket,
    prepare: Callable[[CurrentListingMarket | None], CurrentListingMarket | None],
) -> ListingUpdateStatus:
    """Prepare one partition against the latest read on each conditional attempt."""
    for last_attempt in (False, True):
        current, etag = _read(store)
        prior = current.get(market)
        replacement = prepare(prior)
        if replacement is None:
            return "stale"
        if replacement == prior:
            return "unchanged"
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


def _active_ids(current: CurrentListingMarket) -> dict[ListingKey, str]:
    return {
        row.key: row.instrument_id
        for row in current.slice.rows
        if row.listing_state == "listed" and row.instrument_id is not None
    }


def _current_us_slice(
    current: CurrentListingMarket | None, *, expected_session: date, cutoff: datetime
) -> bool:
    return (
        current is not None
        and current.slice.market == "US"
        and current.slice.session == expected_session
        and cutoff.astimezone(_RAKUTEN_NY).date() == expected_session
        and current.received_at <= cutoff
    )


def _bound_rakuten_scope(
    snapshot: RakutenOrdinarySnapshot,
    *,
    bindings: Iterable[RakutenListingBinding],
    expected_session: date,
    cutoff: datetime,
) -> CurrentRakutenScope:
    if (
        snapshot.catalog_mode != "cash_ordinary_tradable"
        or snapshot.exclusions_received_at is None
        or snapshot.exclusions_reported_count is None
    ):
        raise ValueError("a complete Rakuten catalog and restrictions capture are required")
    if snapshot.effective_session != expected_session:
        raise ValueError("Rakuten capture has a different NY session")
    if any(
        clock.utcoffset() is None or clock > cutoff
        for clock in (snapshot.catalog_received_at, snapshot.exclusions_received_at)
    ):
        raise ValueError("Rakuten receipt is unzoned or after the cutoff")
    available: set[RakutenOrdinaryRow | RakutenRestrictionRow] = {
        *snapshot.rows,
        *snapshot.restriction_only,
    }
    rows: list[RakutenBrokerRow] = []
    for binding in bindings:
        native = binding.observation
        if native not in available or not binding.instrument_id:
            raise ValueError("binding does not identify a unique normalized Rakuten observation")
        if rakuten_listing_key(native.ticker, native.exchange) != binding.listing_key:
            raise ValueError("binding does not match the exact Rakuten listing identity")
        available.remove(native)
        state = native.buy_status if isinstance(native, RakutenOrdinaryRow) else native.restriction
        if state not in _RAKUTEN_STATES:
            raise ValueError("unknown buyability cannot create a broker state")
        rows.append(
            RakutenBrokerRow(
                binding.listing_key,
                binding.instrument_id,
                native.ticker,
                _RAKUTEN_STATES[state],
            )
        )
    return CurrentRakutenScope(
        expected_session,
        snapshot.catalog_received_at,
        snapshot.exclusions_received_at,
        snapshot.reported_count,
        snapshot.exclusions_reported_count,
        tuple(sorted(rows, key=lambda row: row.listing_key)),
    )


def read_current_rakuten_scope(
    current: CurrentListingMarket | None, *, expected_session: date, cutoff: datetime
) -> CurrentRakutenScope | None:
    """Expose broker facts only on the requested current US session and exact active IDs."""
    if cutoff.utcoffset() is None:
        raise ValueError("Rakuten cutoff requires a timezone")
    if current is None or not _current_us_slice(
        current, expected_session=expected_session, cutoff=cutoff
    ):
        return None
    scope = current.rakuten
    if scope is None or scope.effective_session != expected_session or max(scope.clocks) > cutoff:
        return None
    active = _active_ids(current)
    return replace(
        scope,
        rows=tuple(row for row in scope.rows if active.get(row.listing_key) == row.instrument_id),
    )


def refresh_current_us_rakuten_scope(
    store: ListingMasterStore,
    *,
    snapshot: RakutenOrdinarySnapshot | None,
    bindings: Iterable[RakutenListingBinding],
    expected_session: date,
    cutoff: datetime,
) -> ListingUpdateStatus:
    """Replace only broker facts from a complete current pair; never create exchange listings."""
    if cutoff.utcoffset() is None:
        raise ValueError("Rakuten cutoff requires a timezone")
    if snapshot is None:
        raise ValueError("a complete Rakuten catalog and restrictions capture are required")
    scope = _bound_rakuten_scope(
        snapshot, bindings=bindings, expected_session=expected_session, cutoff=cutoff
    )
    native_rows: tuple[RakutenOrdinaryRow | RakutenRestrictionRow, ...] = (
        *snapshot.rows,
        *snapshot.restriction_only,
    )
    captured_keys = {
        key
        for row in native_rows
        if (key := rakuten_listing_key(row.ticker, row.exchange)) is not None
    }
    bound_keys = {row.listing_key for row in scope.rows}
    negatives = {
        (row.listing_key, row.instrument_id, row.state)
        for row in scope.rows
        if row.state != "catalog_target"
    }

    def prepare(current: CurrentListingMarket | None) -> CurrentListingMarket | None:
        if current is None or not _current_us_slice(
            current, expected_session=expected_session, cutoff=cutoff
        ):
            return None
        active = _active_ids(current)
        if any(active.get(row.listing_key) != row.instrument_id for row in scope.rows):
            return None
        if active.keys() & captured_keys != bound_keys:
            raise ValueError("Rakuten bindings must cover all active matched observations")
        previous = current.rakuten
        if previous is not None and previous.effective_session == expected_session:
            if any(new < old for new, old in zip(scope.clocks, previous.clocks, strict=True)):
                return None
            if scope.clocks == previous.clocks and scope != previous:
                raise ValueError("different broker states claim the same Rakuten receipt pair")
            if scope.exclusions_received_at == previous.exclusions_received_at and any(
                (row.listing_key, row.instrument_id, row.state) not in negatives
                for row in previous.rows
                if row.state != "catalog_target"
            ):
                return None  # A newer catalog alone cannot clear a known restriction.
        return replace(current, rakuten=scope)

    return _update_market(store, "US", prepare)
