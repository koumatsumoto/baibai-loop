"""EDINET開示観測の純粋な型。HTTP取得・保存処理は読み込まない。"""

from datetime import date
from math import isfinite

from pydantic import ConfigDict, field_validator
from pydantic.dataclasses import dataclass

from baibai_engine.data_collection.identifiers import normalize_ticker
from baibai_engine.market.metric_quality import TTMQuality


class EDINETProviderError(RuntimeError):
    """Raised when saved EDINET input or its market projection is invalid."""


_MODEL_CONFIG = ConfigDict(strict=True, arbitrary_types_allowed=False)


def _validate_finite(value: float | None) -> float | None:
    if value is not None and not isfinite(value):
        raise ValueError("numeric values must be finite")
    return value


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class EdinetMetricRecord:
    ticker: str
    sales_ttm: float | None = None
    ocf_ttm: float | None = None
    debt: float | None = None
    cash: float | None = None
    investment_securities: float | None = None
    ebitda_ttm: float | None = None
    consolidation_basis: str | None = None
    ttm_quality_ev_ebitda: TTMQuality | None = None
    ttm_quality_p_s: TTMQuality = TTMQuality.UNAVAILABLE
    ttm_quality_pcfr: TTMQuality = TTMQuality.UNAVAILABLE
    operating_profit_ttm: float | None = None
    depreciation_and_amortization_ttm: float | None = None
    capex_ttm: float | None = None
    ocf_receivables_cash_effect: float | None = None
    ocf_inventories_cash_effect: float | None = None
    ocf_payables_cash_effect: float | None = None
    ocf_contract_liabilities_cash_effect: float | None = None
    ocf_advances_received_cash_effect: float | None = None
    ocf_other_payables_cash_effect: float | None = None
    capex_ppe_reported: float | None = None
    capex_intangible_reported: float | None = None
    fcf_ttm: float | None = None
    net_cash: float | None = None
    equity: float | None = None
    total_assets: float | None = None
    ttm_quality_fcf: TTMQuality | None = None
    ttm_quality_net_cash: TTMQuality = TTMQuality.UNAVAILABLE
    snapshot_asof: date | None = None
    snapshot_recorded_at_utc: str | None = None
    source_doc_id: str | None = None
    document_type: str | None = None
    source_submit_datetime: str | None = None
    source_period_start: date | None = None
    source_period_end: date | None = None
    capex_source: str | None = None
    failure_reasons: tuple[str, ...] = ()

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)

    @field_validator(
        "sales_ttm",
        "ocf_ttm",
        "debt",
        "cash",
        "ebitda_ttm",
        "operating_profit_ttm",
        "depreciation_and_amortization_ttm",
        "capex_ttm",
        "ocf_receivables_cash_effect",
        "ocf_inventories_cash_effect",
        "ocf_payables_cash_effect",
        "ocf_contract_liabilities_cash_effect",
        "ocf_advances_received_cash_effect",
        "ocf_other_payables_cash_effect",
        "capex_ppe_reported",
        "capex_intangible_reported",
        "fcf_ttm",
        "net_cash",
        "equity",
        "total_assets",
    )
    @classmethod
    def _finite_numeric_fields(cls, value: float | None) -> float | None:
        return _validate_finite(value)

    @field_validator("investment_securities")
    @classmethod
    def _nonnegative_investment_securities(cls, value: float | None) -> float | None:
        value = _validate_finite(value)
        if value is not None and value < 0:
            raise ValueError("investment_securities must be nonnegative")
        return value

    @field_validator("failure_reasons", mode="before")
    @classmethod
    def _tuple_failure_reasons(
        cls, value: list[str] | tuple[str, ...] | str | None
    ) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value,)
        return tuple(value or ())
