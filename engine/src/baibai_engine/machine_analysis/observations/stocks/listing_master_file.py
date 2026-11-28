"""The one wire format for the current JP and US listings. No remote I/O."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, TypeAdapter

from baibai_engine.data_collection.providers.nasdaq_symbols import (
    US_PRIMARY_VENUES,
    DirectoryFile,
    directory_creation_minute,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    ListingKey,
    ListingMarket,
    ListingSlice,
)
from baibai_engine.machine_analysis.observations.stocks.us_universe import RakutenRestriction
from baibai_engine.primitives.json import encode_json

NASDAQ_DIRECTORY_SOURCE = "nasdaq-trader"
# This one object contains both complete name lists, not a small marker.
LISTING_MASTER_MAX_BYTES = 8_000_000
RAKUTEN_SCOPE_ZONE = ZoneInfo("America/New_York")


type RakutenBrokerState = Literal["catalog_target"] | RakutenRestriction


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
    active_listing_sha256: str
    rows: tuple[RakutenBrokerRow, ...]

    def __post_init__(self) -> None:
        if (
            self.catalog_count < 1
            or self.exclusions_count < 0
            or re.fullmatch(r"[0-9a-f]{64}", self.active_listing_sha256) is None
            or any(
                clock.utcoffset() is None
                or clock.astimezone(RAKUTEN_SCOPE_ZONE).date() != self.effective_session
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


@dataclass(frozen=True, slots=True)
class NasdaqDirectoryVersion:
    """Current provider wall minute plus exact received bytes identity, per file."""

    file: DirectoryFile
    creation_stamp: str
    sha256: str

    @property
    def native_minute(self) -> datetime:
        return directory_creation_minute(self.creation_stamp)


def checked_nasdaq_directory_versions(
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
            checked_nasdaq_directory_versions(
                self.nasdaq_directories,
                market=self.slice.market,
                source=self.source,
                session=self.slice.session,
            ),
        )


def active_us_listing_ids(current: CurrentListingMarket) -> dict[ListingKey, str]:
    return {
        row.key: row.instrument_id
        for row in current.slice.rows
        if row.key[0] in US_PRIMARY_VENUES
        and row.listing_state == "listed"
        and row.instrument_id is not None
    }


def active_us_listing_sha256(active: dict[ListingKey, str]) -> str:
    return sha256(encode_json(sorted(active.items())).encode()).hexdigest()


def read_bound_rakuten_scope(
    current: CurrentListingMarket | None, *, session: date, cutoff: datetime
) -> CurrentRakutenScope | None:
    """Read a matching broker attachment for a session the caller already proved."""
    if cutoff.utcoffset() is None:
        raise ValueError("Rakuten cutoff requires a timezone")
    if (
        current is None
        or current.slice.market != "US"
        or current.slice.session != session
        or session > cutoff.astimezone(RAKUTEN_SCOPE_ZONE).date()
        or current.received_at > cutoff
    ):
        return None
    scope = current.rakuten
    if scope is None or scope.effective_session != session or max(scope.clocks) > cutoff:
        return None
    active = active_us_listing_ids(current)
    if (
        len({row.key for row in current.slice.rows}) != len(current.slice.rows)
        or scope.active_listing_sha256 != active_us_listing_sha256(active)
        or any(active.get(row.listing_key) != row.instrument_id for row in scope.rows)
    ):
        return None
    return scope


_FILE = TypeAdapter(dict[ListingMarket, CurrentListingMarket])


def _require_matching_sections(
    entries: Mapping[ListingMarket, CurrentListingMarket],
) -> None:
    if not entries or any(market != entry.slice.market for market, entry in entries.items()):
        raise ValueError("current listing master does not match its market sections")


def encode_current_listing_master(entries: Mapping[ListingMarket, CurrentListingMarket]) -> bytes:
    """Encode the current private object with market sections in canonical order."""
    _require_matching_sections(entries)
    return _FILE.dump_json(dict(sorted(entries.items())))


def decode_current_listing_master(body: bytes) -> dict[ListingMarket, CurrentListingMarket]:
    """Read the current object; unknown fields must not be lost by an older writer."""
    if len(body) > LISTING_MASTER_MAX_BYTES:
        raise ValueError("current listing master exceeds its byte limit")
    current = _FILE.validate_json(body, strict=True, extra="forbid")
    _require_matching_sections(current)
    return current


def read_local_listing_master(
    path: Path, *, expected_sha256: str | None = None
) -> dict[ListingMarket, CurrentListingMarket]:
    """Read one pin; optionally bind the decoded bytes to an acquisition receipt."""
    with path.open("rb") as stream:
        body = stream.read(LISTING_MASTER_MAX_BYTES + 1)
    if expected_sha256 is not None and sha256(body).hexdigest() != expected_sha256:
        raise ValueError("listing master pin differs from the acquisition handoff")
    return decode_current_listing_master(body)
