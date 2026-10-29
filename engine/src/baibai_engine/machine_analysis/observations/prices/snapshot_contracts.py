"""Owner-declared physical snapshot schema; persisted identity and field semantics are unchanged."""

from __future__ import annotations

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.storage.snapshots.contracts import LakeColumn, LakeDataset

_TEXT = pa.string()
_REAL = pa.float64()

JQUANTS_DAILY_BARS = LakeDataset(
    name="jquants.daily_bars",
    layer="l2_processed",
    contract_version=2,
    sqlite_table="jquants_daily_bars",
    date_column="traded_at",
    columns=(
        LakeColumn("ticker", "TEXT", _TEXT, False, 1),
        LakeColumn("traded_at", "TEXT", _TEXT, False, 2),
        LakeColumn("open", "REAL", _REAL, True),
        LakeColumn("high", "REAL", _REAL, True),
        LakeColumn("low", "REAL", _REAL, True),
        LakeColumn("close", "REAL", _REAL, True),
        LakeColumn("volume", "REAL", _REAL, True),
        LakeColumn("turnover_value", "REAL", _REAL, True),
        LakeColumn("adjustment_factor", "REAL", _REAL, True),
        LakeColumn("upper_limit", "TEXT", _TEXT, True),
        LakeColumn("lower_limit", "TEXT", _TEXT, True),
        LakeColumn("ex_right_type", "TEXT", _TEXT, True),
        # Provider MktCap, in million yen; not recomputed from price and shares.
        LakeColumn("market_cap_million_yen", "REAL", _REAL, True),
    ),
)

JQUANTS_MARKET_CALENDAR = LakeDataset(
    name="jquants.market_calendar",
    layer="l2_processed",
    contract_version=2,
    sqlite_table="jquants_market_calendar",
    date_column="day",
    partition_grain="year",
    population_column=None,
    columns=(
        LakeColumn("day", "TEXT", _TEXT, True, 1),
        LakeColumn("holiday_division", "TEXT", _TEXT, False),
    ),
)
