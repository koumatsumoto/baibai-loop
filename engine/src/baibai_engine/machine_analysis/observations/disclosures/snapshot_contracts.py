"""Owner-declared physical snapshot schema; persisted identity and field semantics are unchanged."""

from __future__ import annotations

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.storage.snapshots.contracts import LakeColumn, LakeDataset

_TEXT = pa.string()
_REAL = pa.float64()
_INTEGER = pa.int64()

JPX_EARNINGS_CALENDAR = LakeDataset(
    name="jpx.earnings_calendar",
    layer="l2_processed",
    sqlite_table="jpx_earnings_calendar",
    date_column="announcement_date",
    partition_grain="year",
    coverage_source="jpx_earnings_calendar",
    columns=(
        LakeColumn("announcement_date", "TEXT", _TEXT, False, 1),
        LakeColumn("ticker", "TEXT", _TEXT, False, 2),
    ),
)

EDINET_DOCUMENTS = LakeDataset(
    name="edinet.documents",
    layer="l2_processed",
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

EDINET_DOCUMENT_LISTS = LakeDataset(
    name="edinet.document_lists",
    layer="l2_processed",
    contract_version=4,
    sqlite_table="edinet_document_lists",
    date_column="doc_date",
    partition_grain="year",
    coverage_authority="unproven",
    population_column=None,
    columns=(
        LakeColumn("doc_date", "TEXT", _TEXT, True, 1),
        LakeColumn("process_datetime", "TEXT", _TEXT, True),
        LakeColumn("result_count", "INTEGER", _INTEGER, False),
        LakeColumn("selected_received_at_utc", "TEXT", _TEXT, True),
        LakeColumn("response_json", "TEXT", _TEXT, True),
        LakeColumn("selected_receipt_id", "TEXT", _TEXT, True),
        LakeColumn("latest_received_at_utc", "TEXT", _TEXT, False),
        LakeColumn("rechecked_asof", "TEXT", _TEXT, True),
    ),
)

JPX_REGULATION_FLAGS = LakeDataset(
    name="jpx.regulation_flags",
    layer="l2_processed",
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
    layer="l2_processed",
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
    layer="l2_processed",
    contract_version=2,
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

TSE_CAPITAL_POLICY_SNAPSHOTS = LakeDataset(
    name="tse.capital_policy_snapshots",
    layer="l2_processed",
    sqlite_table="tse_capital_policy_snapshots",
    date_column="snapshot_month_end",
    partition_grain="year",
    coverage_authority="unproven",
    columns=(
        LakeColumn("snapshot_month_end", "TEXT", _TEXT, False, 1),
        LakeColumn("ticker", "TEXT", _TEXT, False, 2),
        LakeColumn("status", "TEXT", _TEXT, False),
        LakeColumn("status_change", "TEXT", _TEXT, True),
        LakeColumn("updated_on", "TEXT", _TEXT, True),
        LakeColumn("contact_requested", "INTEGER", _INTEGER, False),
        LakeColumn("first_disclosed_month_end", "TEXT", _TEXT, True),
        LakeColumn("first_disclosure_left_censored", "INTEGER", _INTEGER, False),
    ),
)

TENDER_OFFER_EXIT_VALUES = LakeDataset(
    name="edinet.tender_offer_exit_values",
    layer="l2_processed",
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
