"""Owner-declared physical snapshot schema; persisted identity and field semantics are unchanged."""

from __future__ import annotations

import pyarrow as pa  # type: ignore[import-untyped]

from baibai_engine.storage.snapshots.contracts import LakeColumn, LakeDataset

_TEXT = pa.string()
_REAL = pa.float64()
_INTEGER = pa.int64()

JQUANTS_FIN_SUMMARIES = LakeDataset(
    name="jquants.fin_summaries",
    layer="l2_processed",
    sqlite_table="jquants_fin_summaries",
    date_column="disclosed_at",
    partition_grain="year",
    contract_version=5,
    columns=(
        LakeColumn("ticker", "TEXT", _TEXT, False, 1),
        LakeColumn("disclosed_at", "TEXT", _TEXT, False, 2),
        LakeColumn("disclosure_number", "TEXT", _TEXT, False, 3),
        LakeColumn("document_payload", "TEXT", _TEXT, False),
    ),
)

EDINET_METRICS = LakeDataset(
    name="edinet.metrics",
    layer="l2_processed",
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

EDINET_SEGMENT_FACTS = LakeDataset(
    name="edinet.segment_facts",
    layer="l2_processed",
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
    layer="l2_processed",
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
