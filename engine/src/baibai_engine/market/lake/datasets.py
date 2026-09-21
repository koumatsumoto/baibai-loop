"""Concrete L1 contracts for the lake datasets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pyarrow as pa  # type: ignore[import-untyped]

PartitionGrain = Literal["year", "month"]
"""How much of one dataset's history a single partition covers.

The grain is declared per dataset rather than derived from how many rows the store
happens to hold. A reader checks the manifest's layout against this contract, so a
layout that moved with the data would start refusing releases on the day a table grew.
Row counts are the evidence for choosing a grain; they are not the mechanism.
"""

_GRAIN_LAYOUTS: dict[PartitionGrain, tuple[str, ...]] = {
    "month": ("year", "month"),
    "year": ("year",),
}

CoverageAuthority = Literal["daily_bars_rows", "source_coverage", "unproven"]
"""What decides whether a dataset's history is complete rather than merely present.

Completeness is not a property of the rows for most sources: a filing that was never
made and a filing that was never fetched leave the same absence behind, so the answer
has to come from the fetch record. Daily bars are the exception — every trading day owes
a full-market row set, so the rows themselves answer it.

`source_coverage` holds two shapes of record and only one of them can answer the
question. A range claim says a window was fetched, so a span is checkable. A
per-observation claim says which dates carried an observation — measured on the real
store, weekly margin holds 523 single-day intervals and master snapshots 126 — and no
merge of those spans a range, because the days between them are days the source never
published. Proving completeness there would mean knowing the source's publication
calendar, which the store does not hold. Those sources are `unproven`: they can say what
they hold, not that they hold everything.
"""


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
    partition_grain: PartitionGrain = "month"
    coverage_authority: CoverageAuthority = "source_coverage"
    coverage_source: str | None = None
    """The `source_coverage.source` that records this dataset's fetches.

    Usually the SQLite table name, but the two are independent identifiers and have
    already drifted apart once, so the exception is declared rather than assumed.
    """
    population_column: str | None = "ticker"
    """The column whose distinct values are the dataset's population, if it has one."""

    @property
    def coverage_source_name(self) -> str:
        return self.coverage_source or self.sqlite_table

    @property
    def partition_by(self) -> tuple[str, ...]:
        return _GRAIN_LAYOUTS[self.partition_grain]

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


Period = tuple[int, ...]
"""One partition's place in the calendar, ordered as the dataset's layout names it."""


def period_values(dataset: LakeDataset, period: Period) -> dict[str, int]:
    return dict(zip(dataset.partition_by, period, strict=True))


def period_label(period: Period) -> str:
    return "-".join((f"{period[0]:04d}", *(f"{part:02d}" for part in period[1:])))


_TEXT = pa.string()
_REAL = pa.float64()
_INTEGER = pa.int64()

JQUANTS_DAILY_BARS = LakeDataset(
    name="jquants.daily_bars",
    sqlite_table="jquants_daily_bars",
    date_column="traded_at",
    coverage_authority="daily_bars_rows",
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

JQUANTS_WEEKLY_MARGIN = LakeDataset(
    name="jquants.weekly_margin",
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
    ),
)

JQUANTS_MASTER_SNAPSHOTS = LakeDataset(
    name="jquants.master_snapshots",
    sqlite_table="jquants_master_snapshots",
    date_column="snapshot_date",
    partition_grain="year",
    coverage_authority="unproven",
    columns=(
        LakeColumn("snapshot_date", "TEXT", _TEXT, False, 1),
        LakeColumn("ticker", "TEXT", _TEXT, False, 2),
        LakeColumn("name", "TEXT", _TEXT, True),
        LakeColumn("market", "TEXT", _TEXT, True),
        LakeColumn("sector_33", "TEXT", _TEXT, True),
        LakeColumn("is_common_stock", "INTEGER", _INTEGER, True),
    ),
)

JQUANTS_FIN_SUMMARIES = LakeDataset(
    name="jquants.fin_summaries",
    sqlite_table="jquants_fin_summaries",
    date_column="disclosed_at",
    partition_grain="year",
    columns=(
        LakeColumn("ticker", "TEXT", _TEXT, False, 1),
        LakeColumn("disclosed_at", "TEXT", _TEXT, False, 2),
        LakeColumn("forecast_eps", "REAL", _REAL, True),
        LakeColumn("eps_ttm", "REAL", _REAL, True),
        LakeColumn("bps", "REAL", _REAL, True),
        LakeColumn("shares_outstanding", "REAL", _REAL, True),
        LakeColumn("sales", "REAL", _REAL, True),
        LakeColumn("cfo", "REAL", _REAL, True),
        LakeColumn("cash_eq", "REAL", _REAL, True),
        LakeColumn("total_assets", "REAL", _REAL, True),
        LakeColumn("equity", "REAL", _REAL, True),
        LakeColumn("operating_profit", "REAL", _REAL, True),
        LakeColumn("ordinary_profit", "REAL", _REAL, True),
        LakeColumn("profit", "REAL", _REAL, True),
        LakeColumn("forecast_profit", "REAL", _REAL, True),
        LakeColumn("forecast_ordinary_profit", "REAL", _REAL, True),
        LakeColumn("fiscal_period", "TEXT", _TEXT, True),
        LakeColumn("fiscal_year_end", "TEXT", _TEXT, True),
        LakeColumn("period_start", "TEXT", _TEXT, True),
        LakeColumn("period_end", "TEXT", _TEXT, True),
        LakeColumn("dps_actual_annual", "REAL", _REAL, True),
        LakeColumn("dps_forecast_annual", "REAL", _REAL, True),
        LakeColumn("treasury_shares", "REAL", _REAL, True),
        LakeColumn("equity_to_asset_ratio", "REAL", _REAL, True),
        LakeColumn("dividend_q1", "REAL", _REAL, True),
        LakeColumn("dividend_interim", "REAL", _REAL, True),
        LakeColumn("dividend_q3", "REAL", _REAL, True),
        LakeColumn("dividend_year_end", "REAL", _REAL, True),
        LakeColumn("dividend_total_annual", "REAL", _REAL, True),
        LakeColumn("average_shares", "REAL", _REAL, True),
    ),
)

JQUANTS_MARKET_CALENDAR = LakeDataset(
    name="jquants.market_calendar",
    sqlite_table="jquants_market_calendar",
    date_column="day",
    partition_grain="year",
    population_column=None,
    columns=(
        LakeColumn("day", "TEXT", _TEXT, True, 1),
        LakeColumn("is_business_day", "INTEGER", _INTEGER, False),
    ),
)

JPX_EARNINGS_CALENDAR = LakeDataset(
    name="jpx.earnings_calendar",
    sqlite_table="jpx_earnings_calendar",
    date_column="announcement_date",
    partition_grain="year",
    coverage_source="jpx_earnings_calendar",
    columns=(
        LakeColumn("announcement_date", "TEXT", _TEXT, False, 1),
        LakeColumn("ticker", "TEXT", _TEXT, False, 2),
    ),
)

JQUANTS_MARGIN_ALERTS = LakeDataset(
    name="jquants.margin_alerts",
    sqlite_table="jquants_margin_alerts",
    date_column="publication_date",
    partition_grain="year",
    columns=(
        LakeColumn("publication_date", "TEXT", _TEXT, False, 1),
        LakeColumn("ticker", "TEXT", _TEXT, False, 2),
        LakeColumn("applied_date", "TEXT", _TEXT, True),
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

EDINET_DOCUMENTS = LakeDataset(
    name="edinet.documents",
    sqlite_table="edinet_documents",
    date_column="doc_date",
    population_column=None,
    columns=(
        LakeColumn("doc_date", "TEXT", _TEXT, False, 1),
        LakeColumn("sequence_number", "INTEGER", _INTEGER, False, 2),
        LakeColumn("doc_id", "TEXT", _TEXT, False),
        LakeColumn("sec_code", "TEXT", _TEXT, True),
        LakeColumn("doc_type_code", "TEXT", _TEXT, True),
        LakeColumn("csv_flag", "TEXT", _TEXT, True),
        LakeColumn("xbrl_flag", "TEXT", _TEXT, True),
        LakeColumn("legal_status", "TEXT", _TEXT, True),
        LakeColumn("disclosure_status", "TEXT", _TEXT, True),
        LakeColumn("withdrawal_status", "TEXT", _TEXT, True),
        LakeColumn("doc_info_edit_status", "TEXT", _TEXT, True),
        LakeColumn("parent_doc_id", "TEXT", _TEXT, True),
        LakeColumn("operation_datetime", "TEXT", _TEXT, True),
        LakeColumn("submit_datetime", "TEXT", _TEXT, True),
        LakeColumn("doc_description", "TEXT", _TEXT, True),
        LakeColumn("period_start", "TEXT", _TEXT, True),
        LakeColumn("period_end", "TEXT", _TEXT, True),
        LakeColumn("edinet_code", "TEXT", _TEXT, True),
        LakeColumn("issuer_edinet_code", "TEXT", _TEXT, True),
        LakeColumn("subject_edinet_code", "TEXT", _TEXT, True),
    ),
)

EDINET_METRICS = LakeDataset(
    name="edinet.metrics",
    sqlite_table="edinet_metrics",
    date_column="asof_date",
    coverage_authority="unproven",
    contract_version=2,
    columns=(
        LakeColumn("asof_date", "TEXT", _TEXT, False, 1),
        LakeColumn("ticker", "TEXT", _TEXT, False, 2),
        LakeColumn("sales_ttm", "REAL", _REAL, True),
        LakeColumn("ocf_ttm", "REAL", _REAL, True),
        LakeColumn("debt", "REAL", _REAL, True),
        LakeColumn("cash", "REAL", _REAL, True),
        LakeColumn("ebitda_ttm", "REAL", _REAL, True),
        LakeColumn("consolidation_basis", "TEXT", _TEXT, True),
        LakeColumn("ttm_quality_ev_ebitda", "TEXT", _TEXT, True),
        LakeColumn("ttm_quality_p_s", "TEXT", _TEXT, True),
        LakeColumn("ttm_quality_pcfr", "TEXT", _TEXT, True),
        LakeColumn("operating_profit_ttm", "REAL", _REAL, True),
        LakeColumn("depreciation_and_amortization_ttm", "REAL", _REAL, True),
        LakeColumn("capex_ttm", "REAL", _REAL, True),
        LakeColumn("fcf_ttm", "REAL", _REAL, True),
        LakeColumn("net_cash", "REAL", _REAL, True),
        LakeColumn("equity", "REAL", _REAL, True),
        LakeColumn("total_assets", "REAL", _REAL, True),
        LakeColumn("ttm_quality_fcf", "TEXT", _TEXT, True),
        LakeColumn("ttm_quality_net_cash", "TEXT", _TEXT, True),
        LakeColumn("source_doc_id", "TEXT", _TEXT, True),
        LakeColumn("document_type", "TEXT", _TEXT, True),
        LakeColumn("source_submit_datetime", "TEXT", _TEXT, True),
        LakeColumn("source_period_start", "TEXT", _TEXT, True),
        LakeColumn("source_period_end", "TEXT", _TEXT, True),
        LakeColumn("capex_source", "TEXT", _TEXT, True),
        LakeColumn("failure_reasons", "TEXT", _TEXT, True),
        LakeColumn("extractor_revision", "TEXT", _TEXT, True),
        LakeColumn("source_document_revision", "TEXT", _TEXT, True),
        LakeColumn("investment_securities", "REAL", _REAL, True),
        LakeColumn("ocf_receivables_cash_effect", "REAL", _REAL, True),
        LakeColumn("ocf_inventories_cash_effect", "REAL", _REAL, True),
        LakeColumn("ocf_payables_cash_effect", "REAL", _REAL, True),
        LakeColumn("ocf_contract_liabilities_cash_effect", "REAL", _REAL, True),
        LakeColumn("ocf_advances_received_cash_effect", "REAL", _REAL, True),
        LakeColumn("ocf_other_payables_cash_effect", "REAL", _REAL, True),
        LakeColumn("capex_ppe_reported", "REAL", _REAL, True),
        LakeColumn("capex_intangible_reported", "REAL", _REAL, True),
    ),
)

EDINET_DOCUMENT_LISTS = LakeDataset(
    name="edinet.document_lists",
    sqlite_table="edinet_document_lists",
    date_column="doc_date",
    partition_grain="year",
    coverage_authority="unproven",
    population_column=None,
    columns=(
        LakeColumn("doc_date", "TEXT", _TEXT, True, 1),
        LakeColumn("process_datetime", "TEXT", _TEXT, True),
        LakeColumn("result_count", "INTEGER", _INTEGER, False),
        LakeColumn("fetched_at_utc", "TEXT", _TEXT, False),
        LakeColumn("is_final", "INTEGER", _INTEGER, False),
    ),
)

JPX_REGULATION_FLAGS = LakeDataset(
    name="jpx.regulation_flags",
    sqlite_table="jpx_regulation_flags",
    date_column="asof_date",
    partition_grain="year",
    coverage_authority="unproven",
    columns=(
        LakeColumn("asof_date", "TEXT", _TEXT, False, 1),
        LakeColumn("source_name", "TEXT", _TEXT, False, 2),
        LakeColumn("ticker", "TEXT", _TEXT, False, 3),
        LakeColumn("flag", "TEXT", _TEXT, False, 4),
        LakeColumn("fetched_at_utc", "TEXT", _TEXT, True),
    ),
)

JPX_REGULATION_SOURCES = LakeDataset(
    name="jpx.regulation_sources",
    sqlite_table="jpx_regulation_sources",
    date_column="asof_date",
    partition_grain="year",
    coverage_authority="unproven",
    population_column=None,
    columns=(
        LakeColumn("asof_date", "TEXT", _TEXT, False, 1),
        LakeColumn("source_name", "TEXT", _TEXT, False, 2),
        LakeColumn("fetched_at_utc", "TEXT", _TEXT, True),
    ),
)

JPX_DELISTINGS = LakeDataset(
    name="jpx.delistings",
    sqlite_table="jpx_delistings",
    date_column="delisted_on",
    partition_grain="year",
    coverage_authority="unproven",
    columns=(
        LakeColumn("delisted_on", "TEXT", _TEXT, False, 1),
        LakeColumn("ticker", "TEXT", _TEXT, False, 2),
        LakeColumn("name", "TEXT", _TEXT, False),
        LakeColumn("market", "TEXT", _TEXT, True),
        LakeColumn("reason", "TEXT", _TEXT, False),
    ),
)

TENDER_OFFER_EXIT_VALUES = LakeDataset(
    name="edinet.tender_offer_exit_values",
    sqlite_table="tender_offer_exit_values",
    date_column="delisted_on",
    partition_grain="year",
    coverage_authority="unproven",
    columns=(
        LakeColumn("ticker", "TEXT", _TEXT, False, 1),
        LakeColumn("delisted_on", "TEXT", _TEXT, False, 2),
        LakeColumn("offer_price_yen", "REAL", _REAL, False),
        LakeColumn("offer_doc_id", "TEXT", _TEXT, False),
        LakeColumn("result_doc_id", "TEXT", _TEXT, False),
        LakeColumn("filed_on", "TEXT", _TEXT, False),
    ),
)

EDINET_SEGMENT_FACTS = LakeDataset(
    name="edinet.segment_facts",
    sqlite_table="edinet_segment_facts",
    date_column="disclosed_on",
    coverage_authority="unproven",
    columns=(
        LakeColumn("ticker", "TEXT", _TEXT, False),
        LakeColumn("source_doc_id", "TEXT", _TEXT, False, 1),
        LakeColumn("source_submit_datetime", "TEXT", _TEXT, False),
        LakeColumn("disclosed_on", "TEXT", _TEXT, False),
        LakeColumn("source_element", "TEXT", _TEXT, False, 3),
        LakeColumn("source_context", "TEXT", _TEXT, False, 2),
        LakeColumn("issuer_id", "TEXT", _TEXT, False),
        LakeColumn("period_start", "TEXT", _TEXT, True),
        LakeColumn("period_end", "TEXT", _TEXT, False),
        LakeColumn("consolidation_basis", "TEXT", _TEXT, False),
        LakeColumn("segment_axis", "TEXT", _TEXT, False),
        LakeColumn("segment_key", "TEXT", _TEXT, False),
        LakeColumn("segment_name", "TEXT", _TEXT, True),
        LakeColumn("segment_kind", "TEXT", _TEXT, False),
        LakeColumn("metric", "TEXT", _TEXT, False),
        LakeColumn("profit_basis", "TEXT", _TEXT, True),
        LakeColumn("value", "REAL", _REAL, True),
        LakeColumn("currency", "TEXT", _TEXT, False),
        LakeColumn("source_locator", "TEXT", _TEXT, False),
    ),
)

EDINET_DEBT_SCHEDULE = LakeDataset(
    name="edinet.debt_schedule",
    sqlite_table="edinet_debt_schedule",
    date_column="disclosed_on",
    coverage_authority="unproven",
    columns=(
        LakeColumn("ticker", "TEXT", _TEXT, False),
        LakeColumn("source_doc_id", "TEXT", _TEXT, False, 1),
        LakeColumn("source_submit_datetime", "TEXT", _TEXT, False),
        LakeColumn("disclosed_on", "TEXT", _TEXT, False),
        LakeColumn("source_element", "TEXT", _TEXT, False),
        LakeColumn("source_context", "TEXT", _TEXT, False),
        LakeColumn("issuer_id", "TEXT", _TEXT, False),
        LakeColumn("balance_sheet_date", "TEXT", _TEXT, False, 2),
        LakeColumn("consolidation_basis", "TEXT", _TEXT, False, 3),
        LakeColumn("debt_category", "TEXT", _TEXT, False, 4),
        LakeColumn("due_from_months", "INTEGER", _INTEGER, False, 5),
        LakeColumn("due_to_months", "INTEGER", _INTEGER, False, 6),
        LakeColumn("principal", "REAL", _REAL, True),
        LakeColumn("currency", "TEXT", _TEXT, False),
        LakeColumn("source_locator", "TEXT", _TEXT, False),
    ),
)

LAKE_DATASETS = {
    EDINET_DEBT_SCHEDULE.name: EDINET_DEBT_SCHEDULE,
    EDINET_SEGMENT_FACTS.name: EDINET_SEGMENT_FACTS,
    JQUANTS_DAILY_BARS.name: JQUANTS_DAILY_BARS,
    JQUANTS_SHORT_SALE_REPORTS.name: JQUANTS_SHORT_SALE_REPORTS,
    JQUANTS_WEEKLY_MARGIN.name: JQUANTS_WEEKLY_MARGIN,
    JQUANTS_ALL_ISSUES_DAILY_MARGIN.name: JQUANTS_ALL_ISSUES_DAILY_MARGIN,
    JQUANTS_MASTER_SNAPSHOTS.name: JQUANTS_MASTER_SNAPSHOTS,
    JQUANTS_FIN_SUMMARIES.name: JQUANTS_FIN_SUMMARIES,
    JQUANTS_MARKET_CALENDAR.name: JQUANTS_MARKET_CALENDAR,
    JPX_EARNINGS_CALENDAR.name: JPX_EARNINGS_CALENDAR,
    JQUANTS_MARGIN_ALERTS.name: JQUANTS_MARGIN_ALERTS,
    EDINET_DOCUMENTS.name: EDINET_DOCUMENTS,
    EDINET_METRICS.name: EDINET_METRICS,
    EDINET_DOCUMENT_LISTS.name: EDINET_DOCUMENT_LISTS,
    JPX_REGULATION_FLAGS.name: JPX_REGULATION_FLAGS,
    JPX_REGULATION_SOURCES.name: JPX_REGULATION_SOURCES,
    JPX_DELISTINGS.name: JPX_DELISTINGS,
    TENDER_OFFER_EXIT_VALUES.name: TENDER_OFFER_EXIT_VALUES,
}


def require_lake_dataset(name: str) -> LakeDataset:
    try:
        return LAKE_DATASETS[name]
    except KeyError as exc:
        allowed = ", ".join(sorted(LAKE_DATASETS))
        raise ValueError(f"unsupported lake dataset {name!r}; expected one of: {allowed}") from exc
