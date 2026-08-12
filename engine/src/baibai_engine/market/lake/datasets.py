"""Concrete L1 contracts for the two lake pilot datasets."""

from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa  # type: ignore[import-untyped]


@dataclass(frozen=True)
class LakeColumn:
    name: str
    sqlite_type: str
    arrow_type: object
    nullable: bool
    primary_key_ordinal: int = 0


@dataclass(frozen=True)
class LakeDataset:
    name: str
    sqlite_table: str
    date_column: str
    columns: tuple[LakeColumn, ...]
    contract_version: int = 1
    partition_by: tuple[str, ...] = ("year", "month")

    @property
    def primary_key(self) -> tuple[str, ...]:
        return tuple(
            column.name
            for column in sorted(
                self.columns,
                key=lambda item: item.primary_key_ordinal or len(self.columns) + 1,
            )
            if column.primary_key_ordinal
        )

    @property
    def arrow_schema(self) -> object:
        metadata = {
            b"baibai.contract_version": str(self.contract_version).encode(),
            b"baibai.dataset": self.name.encode(),
            b"baibai.primary_key": ",".join(self.primary_key).encode(),
            b"baibai.source_kind": b"legacy_sqlite_import",
        }
        return pa.schema(
            [
                pa.field(column.name, column.arrow_type, nullable=column.nullable)
                for column in self.columns
            ],
            metadata=metadata,
        )


_TEXT = pa.string()
_REAL = pa.float64()
_INTEGER = pa.int64()

JQUANTS_DAILY_BARS = LakeDataset(
    name="jquants.daily_bars",
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
        LakeColumn("adjustment_open", "REAL", _REAL, True),
        LakeColumn("adjustment_high", "REAL", _REAL, True),
        LakeColumn("adjustment_low", "REAL", _REAL, True),
        LakeColumn("adjustment_close", "REAL", _REAL, True),
        LakeColumn("adjustment_volume", "REAL", _REAL, True),
        LakeColumn("adjustment_factor", "REAL", _REAL, True),
        LakeColumn("upper_limit", "TEXT", _TEXT, True),
        LakeColumn("lower_limit", "TEXT", _TEXT, True),
    ),
)

JQUANTS_SHORT_SALE_REPORTS = LakeDataset(
    name="jquants.short_sale_reports",
    sqlite_table="jquants_short_sale_reports",
    date_column="disclosed_at",
    columns=(
        LakeColumn("disclosed_at", "TEXT", _TEXT, False, 1),
        LakeColumn("source_ordinal", "INTEGER", _INTEGER, False, 2),
        LakeColumn("calculated_at", "TEXT", _TEXT, False),
        LakeColumn("ticker", "TEXT", _TEXT, False),
        LakeColumn("short_seller_name", "TEXT", _TEXT, False),
        LakeColumn("discretionary_investment_contractor_name", "TEXT", _TEXT, False),
        LakeColumn("investment_fund_name", "TEXT", _TEXT, False),
        LakeColumn("short_ratio", "REAL", _REAL, True),
        LakeColumn("short_shares", "INTEGER", _INTEGER, True),
        LakeColumn("short_trading_units", "INTEGER", _INTEGER, True),
        LakeColumn("previous_reported_at", "TEXT", _TEXT, True),
        LakeColumn("previous_short_ratio", "REAL", _REAL, True),
        LakeColumn("is_cancellation", "INTEGER", _INTEGER, False),
        LakeColumn("notes", "TEXT", _TEXT, True),
    ),
)

PILOT_DATASETS = {
    JQUANTS_DAILY_BARS.name: JQUANTS_DAILY_BARS,
    JQUANTS_SHORT_SALE_REPORTS.name: JQUANTS_SHORT_SALE_REPORTS,
}


def require_pilot_dataset(name: str) -> LakeDataset:
    try:
        return PILOT_DATASETS[name]
    except KeyError as exc:
        allowed = ", ".join(sorted(PILOT_DATASETS))
        raise ValueError(f"unsupported lake dataset {name!r}; expected one of: {allowed}") from exc
