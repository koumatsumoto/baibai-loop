"""Read Nasdaq Trader's received symbol directories, not prices."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal

type PrimaryUSVenue = Literal["XNAS", "XNYS", "XASE", "ARCX", "BATS"]
type DirectoryFile = Literal["nasdaqlisted.txt", "otherlisted.txt"]

# Nasdaq Trader's otherlisted listing codes, not the execution venue of a quote.
_OTHER_LISTED: dict[str, PrimaryUSVenue] = {
    "N": "XNYS",
    "A": "XASE",  # NYSE American, formerly NYSE MKT.
    "P": "ARCX",
    "Z": "BATS",  # Cboe BZX Equities, not Cboe's other venues.
}
US_PRIMARY_VENUES: frozenset[PrimaryUSVenue] = frozenset({"XNAS", *_OTHER_LISTED.values()})

_CREATION_MINUTE = r"[0-9]{10}:[0-9]{2}"
_DEPOSITARY_DESCRIPTION = re.compile(
    r"\b(?:deposit(?:ary|ory) (?:shares?|receipts?)|(?:un)?sponsored AD[RS]s?)\b", re.I
)

_HEADERS: dict[DirectoryFile, str] = {
    "nasdaqlisted.txt": (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|"
        "ETF|NextShares"
    ),
    "otherlisted.txt": (
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol"
    ),
}


@dataclass(frozen=True, slots=True)
class USListing:
    venue: PrimaryUSVenue
    ticker: str
    security_name: str
    fund_flag: bool

    @property
    def declared_depositary(self) -> bool:
        # The directory name may explicitly identify a non-ordinary share.
        # Absence of this description does not prove the product is an ordinary share.
        return _DEPOSITARY_DESCRIPTION.search(self.security_name) is not None


@dataclass(frozen=True, slots=True)
class USSymbolDirectory:
    file: DirectoryFile
    creation_stamp: str  # Native clock; not a session date or a timestamp of receipt.
    listings: tuple[USListing, ...]


def directory_creation_minute(stamp: str) -> datetime:
    """Read the provider wall minute, with no inferred timezone or session scope."""
    if re.fullmatch(_CREATION_MINUTE, stamp) is None:
        raise ValueError("invalid Nasdaq Trader file creation record")
    try:
        day = date(int(stamp[4:8]), int(stamp[:2]), int(stamp[2:4]))
        minute = time(int(stamp[8:10]), int(stamp[11:13]))
    except ValueError as error:
        raise ValueError("invalid Nasdaq Trader file creation record") from error
    return datetime.combine(day, minute)


def _creation_stamp(line: str) -> str:
    match = re.fullmatch(rf"File Creation Time: ({_CREATION_MINUTE})\|+", line)
    if match is None:
        raise ValueError("missing Nasdaq Trader file creation record")
    stamp = match[1]
    directory_creation_minute(stamp)
    return stamp


def read_us_symbol_directory(body: bytes, *, file: DirectoryFile) -> USSymbolDirectory:
    """Read supported primary listings from an already received original."""
    lines = body.decode("utf-8").splitlines()
    if len(lines) < 3 or lines[0] != _HEADERS[file]:
        raise ValueError("unexpected Nasdaq Trader symbol directory header")
    stamp = _creation_stamp(lines[-1])
    listings: dict[tuple[PrimaryUSVenue, str], USListing] = {}
    for line in lines[1:-1]:
        cells = line.split("|")
        if len(cells) != 8:
            raise ValueError("invalid Nasdaq Trader symbol directory row")
        venue: PrimaryUSVenue | None
        if file == "nasdaqlisted.txt":
            ticker, name, tier, test, _, _, etf, nextshares = cells
            if tier not in ("Q", "G", "S"):
                raise ValueError("unknown Nasdaq listing tier")
            venue = "XNAS"
        else:
            # Use the CQS symbol, not the ACT reporting or Nasdaq routing symbol.
            _, name, exchange, ticker, etf, _, test, _ = cells
            if re.fullmatch(r"[A-Z]", exchange) is None:
                raise ValueError("invalid other-listed exchange code")
            venue = _OTHER_LISTED.get(exchange)
            nextshares = "N"
        if any(flag not in ("Y", "N") for flag in (test, etf, nextshares)):
            raise ValueError("invalid Nasdaq Trader issue flag")
        if test == "Y" or venue is None:
            continue
        name = name.strip()
        if not ticker or not ticker.isprintable() or not name or not name.isprintable():
            raise ValueError("incomplete Nasdaq Trader primary listing")
        key = (venue, ticker)
        if key in listings:
            raise ValueError("duplicate Nasdaq Trader primary listing")
        listings[key] = USListing(
            venue=venue, ticker=ticker, security_name=name, fund_flag="Y" in (etf, nextshares)
        )
    if not listings:
        raise ValueError("Nasdaq Trader directory has no supported primary listings")
    return USSymbolDirectory(file=file, creation_stamp=stamp, listings=tuple(listings.values()))
