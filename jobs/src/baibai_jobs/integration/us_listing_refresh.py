"""Refresh the private US exchange-listing section, not broker or price readiness."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from hashlib import sha256
from typing import Literal
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.nasdaq_symbols import (
    DirectoryFile,
    read_us_symbol_directory,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master import VerifiedListingEvent
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    NASDAQ_DIRECTORY_SOURCE,
    NasdaqDirectoryVersion,
)
from baibai_engine.machine_analysis.observations.stocks.us_listing_input import (
    normalize_us_listings,
)
from baibai_jobs.integration.listing_master_current import (
    ListingMasterStore,
    ListingUpdateStatus,
    refresh_current_listing_master,
)
from baibai_jobs.schedule import us_master_refresh_date

_US_PRIMARY_MIC = frozenset({"XNAS", "XNYS"})

type USListingRefreshStatus = ListingUpdateStatus | Literal["closed"]


@dataclass(frozen=True, slots=True)
class USListingDirectoryReceipt:
    """An authorized receipt; acquisition independently establishes session and completeness.

    Neither the native footer nor the arrival date alone establishes that the source data
    covers the intended session. The owner supplying this input checks that first.
    """

    file: DirectoryFile
    source_session: date
    received_at: datetime
    complete: bool
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class USListingInputs:
    receipts: tuple[USListingDirectoryReceipt, ...]
    verified_events: tuple[VerifiedListingEvent, ...] = ()


def refresh_current_us_listings(
    store: ListingMasterStore,
    *,
    created_at: datetime,
    now: Callable[[], datetime],
    scheduled: bool,
    is_open: Callable[[date], bool | None],
    load_verified: Callable[[date], USListingInputs],
) -> USListingRefreshStatus:
    """Apply complete current XNAS/XNYS directories through the existing Market owner."""
    target = us_master_refresh_date(created_at, now(), scheduled=scheduled, is_open=is_open)
    if target is None:
        return "closed"
    inputs = load_verified(target)
    new_york = ZoneInfo("America/New_York")
    if any(
        receipt.complete is not True
        or receipt.source_session != target
        or receipt.received_at.utcoffset() is None
        or receipt.received_at.astimezone(new_york).date() != target
        for receipt in inputs.receipts
    ):
        raise ValueError("US listing directories require complete receipts for the current NY day")
    directories = tuple(
        read_us_symbol_directory(receipt.body, file=receipt.file) for receipt in inputs.receipts
    )
    observations = normalize_us_listings(directories, complete=True)
    versions = tuple(
        NasdaqDirectoryVersion(
            directory.file, directory.creation_stamp, sha256(receipt.body).hexdigest()
        )
        for directory, receipt in zip(directories, inputs.receipts, strict=True)
    )
    # This rules out a cache from a different day; it does not establish session scope.
    if any(version.native_minute.date() != target for version in versions):
        raise ValueError("US listing directory native creation date does not match the session")
    if any(
        event.key[0] not in _US_PRIMARY_MIC
        or (event.previous_key is not None and event.previous_key[0] not in _US_PRIMARY_MIC)
        for event in inputs.verified_events
    ):
        raise ValueError("US listing notice is outside the XNAS/XNYS scope")

    received_at = max(r.received_at.astimezone(UTC) for r in inputs.receipts)
    checked_at = now()
    if us_master_refresh_date(created_at, checked_at, scheduled=scheduled, is_open=is_open) is None:
        return "closed"
    if received_at > checked_at.astimezone(UTC):
        raise ValueError("US listing receipt is after the current refresh")
    return refresh_current_listing_master(
        store,
        market="US",
        session=target,
        source=NASDAQ_DIRECTORY_SOURCE,
        received_at=received_at,
        observations=observations,
        complete=True,
        verified_events=inputs.verified_events,
        nasdaq_directories=versions,
    )
