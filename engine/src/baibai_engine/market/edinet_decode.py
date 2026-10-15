"""Pure EDINET field decoding shared by HTTP adapters, CSV parsing and SQLite reads."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from baibai_engine.market.edinet_models import EdinetMetricRecord, EDINETProviderError
from baibai_engine.market.metric_quality import TTMQuality
from baibai_engine.market.ticker import normalize_ticker


def normalize_metric_record(record: Mapping[str, Any]) -> EdinetMetricRecord:
    # `or` チェーンは 0.0 (cash=0、debt=0 等の正当な値) を falsy 扱いするため、
    # キー存在ベースの coalesce で最初の非 None 値を取る。
    return EdinetMetricRecord(
        ticker=parse_sec_code(coalesce_field(record, "secCode", "ticker", "code")),
        sales_ttm=to_float(coalesce_field(record, "sales_ttm", "SalesTTM")),
        ocf_ttm=to_float(coalesce_field(record, "ocf_ttm", "OperatingCashFlowTTM")),
        debt=to_float(coalesce_field(record, "debt", "Debt")),
        cash=to_float(coalesce_field(record, "cash", "Cash")),
        investment_securities=to_float(
            coalesce_field(record, "investment_securities", "InvestmentSecurities")
        ),
        ebitda_ttm=to_float(coalesce_field(record, "ebitda_ttm", "EBITDATTM")),
        consolidation_basis=coalesce_field(record, "consolidation_basis", "ConsolidationBasis"),
        ttm_quality_ev_ebitda=_parse_optional_ttm_quality(
            coalesce_field(record, "ttm_quality_ev_ebitda", "TTMQualityEvEbitda")
        ),
        ttm_quality_p_s=_parse_ttm_quality(
            coalesce_field(record, "ttm_quality_p_s", "TTMQualityPS")
        ),
        ttm_quality_pcfr=_parse_ttm_quality(
            coalesce_field(record, "ttm_quality_pcfr", "TTMQualityPCFR")
        ),
        operating_profit_ttm=to_float(coalesce_field(record, "operating_profit_ttm")),
        depreciation_and_amortization_ttm=to_float(
            coalesce_field(record, "depreciation_and_amortization_ttm")
        ),
        capex_ttm=to_float(coalesce_field(record, "capex_ttm")),
        ocf_receivables_cash_effect=to_float(coalesce_field(record, "ocf_receivables_cash_effect")),
        ocf_inventories_cash_effect=to_float(coalesce_field(record, "ocf_inventories_cash_effect")),
        ocf_payables_cash_effect=to_float(coalesce_field(record, "ocf_payables_cash_effect")),
        ocf_contract_liabilities_cash_effect=to_float(
            coalesce_field(record, "ocf_contract_liabilities_cash_effect")
        ),
        ocf_advances_received_cash_effect=to_float(
            coalesce_field(record, "ocf_advances_received_cash_effect")
        ),
        ocf_other_payables_cash_effect=to_float(
            coalesce_field(record, "ocf_other_payables_cash_effect")
        ),
        capex_ppe_reported=to_float(coalesce_field(record, "capex_ppe_reported")),
        capex_intangible_reported=to_float(coalesce_field(record, "capex_intangible_reported")),
        fcf_ttm=to_float(coalesce_field(record, "fcf_ttm")),
        net_cash=to_float(coalesce_field(record, "net_cash")),
        equity=to_float(coalesce_field(record, "equity")),
        total_assets=to_float(coalesce_field(record, "total_assets")),
        ttm_quality_fcf=_parse_optional_ttm_quality(coalesce_field(record, "ttm_quality_fcf")),
        ttm_quality_net_cash=_parse_ttm_quality(coalesce_field(record, "ttm_quality_net_cash")),
        source_doc_id=text_or_none(coalesce_field(record, "source_doc_id")),
        document_type=text_or_none(coalesce_field(record, "document_type")),
        source_submit_datetime=text_or_none(coalesce_field(record, "source_submit_datetime")),
        source_period_start=parse_optional_date(coalesce_field(record, "source_period_start")),
        source_period_end=parse_optional_date(coalesce_field(record, "source_period_end")),
        capex_source=text_or_none(coalesce_field(record, "capex_source")),
        failure_reasons=tuple(coalesce_field(record, "failure_reasons") or ()),
    )


def parse_sec_code(sec_code: Any) -> str:
    raw = str(sec_code or "").strip()
    if len(raw) == 5:
        if raw[-1] != "0":
            raise EDINETProviderError(
                f"unsupported EDINET secCode suffix: {safe_error_value(sec_code)}"
            )
        raw = raw[:4]
    try:
        return normalize_ticker(raw)
    except ValueError as exc:
        raise EDINETProviderError(f"invalid EDINET secCode: {safe_error_value(sec_code)}") from exc


def safe_error_value(value: object, *, max_length: int = 80) -> str:
    text = str(value)
    sanitized = "".join(
        char if char.isprintable() and char not in "\r\n\t" else "?" for char in text
    )
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "..."
    return repr(sanitized)


def coalesce_field(record: Mapping[str, Any], *keys: str) -> Any:
    """Return first key with a non-None/non-empty value, preserving legitimate zeros."""
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def text_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _parse_optional_ttm_quality(value: Any) -> TTMQuality | None:
    try:
        return TTMQuality(value)
    except (ValueError, TypeError):
        return None


def _parse_ttm_quality(value: Any) -> TTMQuality:
    raw = str(value or TTMQuality.UNAVAILABLE.value)
    try:
        return TTMQuality(raw)
    except ValueError:
        return TTMQuality.UNAVAILABLE


def parse_optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def to_float(value: Any) -> float | None:
    if value in (None, "", "-", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
