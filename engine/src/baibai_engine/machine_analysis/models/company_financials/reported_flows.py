"""現行モデルの報告フロー契約。保存済み世代や物理列の名称は変更しない。"""

from __future__ import annotations

from dataclasses import asdict

from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    FinancialSnapshot,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EdinetMetricRecord
from baibai_engine.machine_analysis.observations.financials.metric_quality import TTMQuality

# 旧物理列は reported amount を保持している。現行モデルでは同値の TTM alias を出さない。
_EDINET_FLOW_FIELDS = (
    "edinet_ocf_ttm",
    "capex_ttm",
    "fcf_ttm",
    "ebitda_ttm",
    "depreciation_and_amortization_ttm",
)


def financial_observation_payload(
    snapshot: FinancialSnapshot, edinet: EdinetMetricRecord | None
) -> dict[str, object]:
    """報告値を残し、12か月が成立しない額を年間倍率の分母・分子にしない。"""
    result: dict[str, object] = asdict(snapshot)
    for name in _EDINET_FLOW_FIELDS:
        result.pop(name)
    result["edinet_reported_flows"] = _reported_flows(edinet)
    if snapshot.ttm_quality_fcf_yield != TTMQuality.EXACT:
        result["fcf_yield"] = None
    if snapshot.ttm_quality_ev_ebitda != TTMQuality.EXACT:
        result["ev_ebitda"] = None
    return result


def _reported_flows(record: EdinetMetricRecord | None) -> dict[str, object] | None:
    if record is None:
        return None
    kind = {
        "120": "annual",
        "130": "annual",
        "140": "year_to_date",
        "150": "year_to_date",
        "160": "interim",
        "170": "interim",
    }.get(record.document_type or "", "unknown")
    return {
        "period_kind": kind,
        "amount_unit": "JPY",
        "amounts": {
            "sales": record.sales_ttm,
            "operating_cash_flow": record.ocf_ttm,
            "operating_profit": record.operating_profit_ttm,
            "depreciation_and_amortization": record.depreciation_and_amortization_ttm,
            "ebitda": record.ebitda_ttm,
            "capex": record.capex_ttm,
            "free_cash_flow": record.fcf_ttm,
            # 部分内訳は総額の説明材料。合算や正常化をせず、欠測と報告符号を保持する。
            "ocf_receivables_cash_effect": record.ocf_receivables_cash_effect,
            "ocf_inventories_cash_effect": record.ocf_inventories_cash_effect,
            "ocf_payables_cash_effect": record.ocf_payables_cash_effect,
            "ocf_contract_liabilities_cash_effect": record.ocf_contract_liabilities_cash_effect,
            "ocf_advances_received_cash_effect": record.ocf_advances_received_cash_effect,
            "ocf_other_payables_cash_effect": record.ocf_other_payables_cash_effect,
            "capex_ppe_reported": record.capex_ppe_reported,
            "capex_intangible_reported": record.capex_intangible_reported,
        },
        # 既存metricはcontext日付を持たず、年度metadataのexactを実測期間へ読み替えない。
        "measurement_period": {
            "start": None,
            "end": None,
            "reason": "fact_dates_not_retained",
        },
        "filing_fiscal_period": {
            "start": record.source_period_start,
            "end": record.source_period_end,
        },
        "consolidation_basis": record.consolidation_basis,
        "source_doc_id": record.source_doc_id,
        "annual_quality": {
            "sales": record.ttm_quality_p_s,
            "operating_cash_flow": record.ttm_quality_pcfr,
            "ebitda": record.ttm_quality_ev_ebitda,
            "free_cash_flow": record.ttm_quality_fcf,
        },
        "annual_quality_basis": "annual_filing_metadata_not_native_fact_dates",
    }
