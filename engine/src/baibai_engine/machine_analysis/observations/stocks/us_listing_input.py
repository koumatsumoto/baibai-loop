"""Convert both validated Nasdaq Trader files to the shared listing input."""

from __future__ import annotations

from collections.abc import Iterable

from baibai_engine.data_collection.providers.nasdaq_symbols import (
    US_PRIMARY_VENUES,
    DirectoryFile,
    PrimaryUSVenue,
    USSymbolDirectory,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master import ObservedListing

_VENUES: dict[DirectoryFile, frozenset[PrimaryUSVenue]] = {
    "nasdaqlisted.txt": frozenset({"XNAS"}),
    "otherlisted.txt": US_PRIMARY_VENUES - {"XNAS"},
}


def normalize_us_listings(
    directories: Iterable[USSymbolDirectory], *, complete: bool
) -> tuple[ObservedListing, ...]:
    """Completeness/session coherence come from acquisition, not a native footer."""
    sources = tuple(directories)
    if (
        complete is not True
        or len(sources) != len(_VENUES)
        or {s.file for s in sources} != set(_VENUES)
    ):
        raise ValueError("current US slice requires both complete symbol directories")
    rows: list[ObservedListing] = []
    for source in sources:
        venues = _VENUES[source.file]
        if not source.listings or any(row.venue not in venues for row in source.listings):
            raise ValueError("US listing directory is empty or has the wrong venue")
        rows.extend(
            ObservedListing(
                (row.venue, row.ticker),
                row.security_name,
                "fund" if row.fund_flag else "not_flagged_fund",
            )
            for row in source.listings
        )
    return tuple(rows)
