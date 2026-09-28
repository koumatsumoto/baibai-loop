"""銘柄view間で共有する表示変換。"""

from __future__ import annotations

from collections.abc import Mapping

from baibai_engine.read_api import PortfolioLedgerError, PortfolioSnapshot
from baibai_web.readmodel.models import (
    PortfolioState,
)
from baibai_web.sources.protocols import (
    LedgerSource,
)


def _mapping_items_optional(value: object) -> list[Mapping[str, object]]:
    return [] if value is None else _mapping_items(value)


def _mapping_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError("expected an array of objects")
    return [item for item in value if isinstance(item, Mapping)]


def _safe_snapshot(ledger: LedgerSource) -> PortfolioSnapshot | None:
    if not ledger.exists():
        return None
    try:
        return ledger.snapshot()
    except PortfolioLedgerError:
        return None


def _held_and_reserved_tickers(ledger: LedgerSource) -> tuple[set[str], set[str]]:
    snapshot = _safe_snapshot(ledger)
    if snapshot is None:
        return set(), set()
    held = {item.ticker for item in snapshot.holdings}
    reserved = {item.ticker for item in snapshot.active_reservations}
    return held, reserved


def _portfolio_state(ticker: str, *, held: set[str], reserved: set[str]) -> PortfolioState:
    is_held = ticker in held
    is_reserved = ticker in reserved
    if is_held and is_reserved:
        return "held_and_reserved"
    if is_held:
        return "held"
    if is_reserved:
        return "reserved"
    return "unheld"


def _mapping_optional(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
