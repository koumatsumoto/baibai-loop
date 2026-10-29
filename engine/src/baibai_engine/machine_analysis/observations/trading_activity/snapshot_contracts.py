"""Owner-declared physical snapshot schema; persisted identity and field semantics are unchanged."""

from __future__ import annotations

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.storage.snapshots.contracts import LakeColumn, LakeDataset

_TEXT = pa.string()
_REAL = pa.float64()
_INTEGER = pa.int64()

JQUANTS_SHORT_SALE_REPORTS = LakeDataset(
    name="jquants.short_sale_reports",
    layer="l2_processed",
    contract_version=2,
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
        LakeColumn("notes", "TEXT", _TEXT, True),
        LakeColumn("short_seller_address", "TEXT", _TEXT, True),
        LakeColumn("discretionary_investment_contractor_address", "TEXT", _TEXT, True),
    ),
)

JQUANTS_WEEKLY_MARGIN = LakeDataset(
    name="jquants.weekly_margin",
    layer="l2_processed",
    sqlite_table="jquants_weekly_margin",
    date_column="week_end",
    coverage_authority="unproven",
    columns=(
        LakeColumn("week_end", "TEXT", _TEXT, False, 1),
        LakeColumn("ticker", "TEXT", _TEXT, False, 2),
        LakeColumn("long_vol", "REAL", _REAL, True),
        LakeColumn("short_vol", "REAL", _REAL, True),
        LakeColumn("long_std_vol", "REAL", _REAL, True),
        LakeColumn("long_neg_vol", "REAL", _REAL, True),
        LakeColumn("short_std_vol", "REAL", _REAL, True),
        LakeColumn("short_neg_vol", "REAL", _REAL, True),
        LakeColumn("issue_type", "TEXT", _TEXT, True),
    ),
)

JQUANTS_ALL_ISSUES_DAILY_MARGIN = LakeDataset(
    name="jquants.all_issues_daily_margin",
    layer="l2_processed",
    contract_version=2,
    sqlite_table="jquants_all_issues_daily_margin",
    date_column="balance_date",
    coverage_authority="unproven",
    columns=(
        LakeColumn("balance_date", "TEXT", _TEXT, False, 1),
        LakeColumn("ticker", "TEXT", _TEXT, False, 2),
        LakeColumn("long_vol", "REAL", _REAL, True),
        LakeColumn("short_vol", "REAL", _REAL, True),
        LakeColumn("long_std_vol", "REAL", _REAL, True),
        LakeColumn("long_neg_vol", "REAL", _REAL, True),
        LakeColumn("short_std_vol", "REAL", _REAL, True),
        LakeColumn("short_neg_vol", "REAL", _REAL, True),
        LakeColumn("issue_type", "TEXT", _TEXT, True),
        LakeColumn("publication_date", "TEXT", _TEXT, True),
        LakeColumn("long_value", "REAL", _REAL, True),
        LakeColumn("short_value", "REAL", _REAL, True),
        LakeColumn("long_std_value", "REAL", _REAL, True),
        LakeColumn("long_neg_value", "REAL", _REAL, True),
        LakeColumn("short_std_value", "REAL", _REAL, True),
        LakeColumn("short_neg_value", "REAL", _REAL, True),
    ),
)

JQUANTS_MARGIN_ALERTS = LakeDataset(
    name="jquants.margin_alerts",
    layer="l2_processed",
    sqlite_table="jquants_margin_alerts",
    date_column="publication_date",
    partition_grain="year",
    contract_version=2,
    columns=(
        LakeColumn("publication_date", "TEXT", _TEXT, False, 1),
        LakeColumn("ticker", "TEXT", _TEXT, False, 2),
        LakeColumn("applied_date", "TEXT", _TEXT, False, 3),
        LakeColumn("publication_reason", "TEXT", _TEXT, True),
        LakeColumn("short_outstanding", "REAL", _REAL, True),
        LakeColumn("short_change", "REAL", _REAL, True),
        LakeColumn("short_ratio", "REAL", _REAL, True),
        LakeColumn("long_outstanding", "REAL", _REAL, True),
        LakeColumn("long_change", "REAL", _REAL, True),
        LakeColumn("long_ratio", "REAL", _REAL, True),
        LakeColumn("short_long_ratio", "REAL", _REAL, True),
        LakeColumn("short_negotiable_outstanding", "REAL", _REAL, True),
        LakeColumn("short_negotiable_change", "REAL", _REAL, True),
        LakeColumn("short_standard_outstanding", "REAL", _REAL, True),
        LakeColumn("short_standard_change", "REAL", _REAL, True),
        LakeColumn("long_negotiable_outstanding", "REAL", _REAL, True),
        LakeColumn("long_negotiable_change", "REAL", _REAL, True),
        LakeColumn("long_standard_outstanding", "REAL", _REAL, True),
        LakeColumn("long_standard_change", "REAL", _REAL, True),
        LakeColumn("tse_margin_regulation_classification", "TEXT", _TEXT, True),
    ),
)
