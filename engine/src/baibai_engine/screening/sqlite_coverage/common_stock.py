"""Exact-date ordinary-stock scope shared by SQLite coverage checks."""

from __future__ import annotations

from datetime import date

from baibai_engine.market.universe import (
    ELIGIBLE_MARKETS,
    NON_COMMON_STOCK_SECTORS,
    TSE_33_SECTORS,
)

_MARKETS = tuple(sorted(ELIGIBLE_MARKETS))
_SECTORS = tuple(sorted(TSE_33_SECTORS))
_KNOWN_SECTORS = tuple(sorted(TSE_33_SECTORS | NON_COMMON_STOCK_SECTORS))


def _named_values(prefix: str, values: tuple[str, ...]) -> str:
    return ", ".join(f":{prefix}{index}" for index in range(len(values)))


# The stored acquisition flag alone does not distinguish ordinary stocks from
# other instruments. Classify this population by market/sector, retaining stocks
# with missing bars or financials so coverage gaps remain visible.
# docs/reference/screening-runtime.md#input-scope
COMMON_STOCK_SNAPSHOT_SQL = (
    "SELECT ticker FROM jquants_master_snapshots WHERE snapshot_date = :asof "  # nosec B608
    f"AND UPPER(market) IN ({_named_values('market', _MARKETS)}) "
    f"AND sector_33 IN ({_named_values('sector', _SECTORS)})"
)

UNKNOWN_ELIGIBLE_SECTOR_SQL = (
    "SELECT COUNT(*) FROM jquants_master_snapshots WHERE snapshot_date = :asof "  # nosec B608
    f"AND UPPER(market) IN ({_named_values('market', _MARKETS)}) "
    f"AND sector_33 NOT IN ({_named_values('known_sector', _KNOWN_SECTORS)})"
)


def scope_parameters(asof: date) -> dict[str, str]:
    return {
        "asof": asof.isoformat(),
        **{f"market{index}": value for index, value in enumerate(_MARKETS)},
        **{f"sector{index}": value for index, value in enumerate(_SECTORS)},
        **{f"known_sector{index}": value for index, value in enumerate(_KNOWN_SECTORS)},
    }
