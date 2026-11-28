"""Read only the pinned current JP listing section for a proven calendar basis."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from sqlite3 import Connection
from typing import Protocol

from pydantic import ValidationError

from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.prices.calendar_context import (
    observation_calendar_context,
)
from baibai_engine.machine_analysis.observations.stocks.contracts import MasterSnapshot
from baibai_engine.machine_analysis.observations.stocks.listing_master import ListingSlice
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import (
    project_primary_security,
)
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.primitives.time import JST


class CurrentListingSection(Protocol):
    @property
    def slice(self) -> ListingSlice: ...

    @property
    def received_at(self) -> datetime: ...


def read_current_jp_master(
    current: CurrentListingSection | None,
    *,
    calendar: Connection,
    as_of: date,
    evaluation_time: datetime,
    tickers: Sequence[str] | None = None,
) -> MasterSnapshot | None:
    """Project a pinned file section and the caller's sealed cash-equity calendar.

    Select the session before narrowing to tickers. No calendar basis, wrong
    session or later receipt is unavailable; a valid empty selection is not.
    """
    if evaluation_time.utcoffset() is None:
        raise ValueError("current JP master requires an aware evaluation time")
    if current is None or current.slice.market != "JP":
        return None
    received_at = current.received_at
    if received_at.utcoffset() is None:
        raise ValueError("current JP master requires an aware receipt")
    if as_of > evaluation_time.astimezone(JST).date() or received_at.astimezone(
        UTC
    ) > evaluation_time.astimezone(UTC):
        return None
    try:
        session, _ = observation_calendar_context(calendar, as_of)
    except ValueError:
        return None
    if current.slice.session != session:
        return None

    codes = None if tickers is None else {f"{normalize_ticker(ticker)}0" for ticker in tickers}
    securities = {}
    for row in current.slice.rows:
        mic, code = row.key
        if mic != "XTKS" or row.listing_state != "listed":
            continue
        if len(code) != 5 or not (code.isascii() and code.isalnum()) or code != code.upper():
            raise JQuantsProviderError("current JP master requires an exact five-character Code")
        if codes is not None and code not in codes:
            continue
        jp = row.japan
        if jp is None or not jp.product_category.strip():
            raise JQuantsProviderError("current JP master requires JP attributes")
        try:
            security = project_primary_security(
                code,
                name=row.security_name,
                market_segment=jp.market_segment,
                sector_33=jp.sector_33,
                product_category=jp.product_category,
            )
        except ValidationError as error:
            raise JQuantsProviderError("invalid current JP master security fields") from error
        if security is not None:
            securities[security.ticker] = security
    return MasterSnapshot(session, dict(sorted(securities.items())))
