"""保存済みStock Analysisの機械推計・CF文脈を投影する。再計算・選抜は行わない。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

from .estimates import EXPECTED_RETURN_MODEL_VERSION

CARRY_MISSING_ASSUMPTION = (
    "Missing dividend_yield and net_share_change_yoy contribute zero to machine carry; "
    "missing inputs are not observed zero dividends or unchanged shares."
)


@dataclass(frozen=True, slots=True)
class MachineValuation:
    """調査した Projection と区別する倍率/自己価格レンジの機械 anchor。"""

    sector_median_anchor_yen: float | None
    self_range_anchor_yen: float | None
    er_annual: float | None
    reversion_annual: float | None
    carry_annual: float | None
    anchor_metrics: str | None
    model_version: str | None
    unit: str | None
    assumptions: str | None
    market_price_yen: float | None
    dividend_yield: float | None
    dividend_basis: str | None
    net_share_change_yoy: float | None
    carry_missing_assumption: str | None


def machine_valuation_from_analysis(analysis: Mapping[str, object]) -> MachineValuation:
    """Read only saved estimates; a missing coordinate remains missing."""
    raw = analysis.get("metrics")
    metrics = raw if isinstance(raw, Mapping) else {}
    return MachineValuation(
        sector_median_anchor_yen=_number(metrics.get("fv_sector_median_yen")),
        self_range_anchor_yen=_number(metrics.get("fv_self_range_yen")),
        er_annual=_number(metrics.get("er_annual")),
        reversion_annual=_number(metrics.get("er_reversion_annual")),
        carry_annual=_number(metrics.get("er_carry_annual")),
        anchor_metrics=_text(metrics.get("er_anchor_metrics")),
        model_version=_text(metrics.get("er_model_version")),
        unit=_text(metrics.get("er_unit")),
        assumptions=_text(metrics.get("er_assumptions")),
        market_price_yen=_number(metrics.get("market_price_yen")),
        dividend_yield=_number(metrics.get("dividend_yield")),
        dividend_basis=_text(metrics.get("dividend_basis")),
        net_share_change_yoy=_number(metrics.get("net_share_change_yoy")),
        carry_missing_assumption=CARRY_MISSING_ASSUMPTION
        if metrics.get("er_model_version") == EXPECTED_RETURN_MODEL_VERSION
        else None,
    )


def anchor_gap_pct(valuation: MachineValuation) -> float | None:
    anchor, price = valuation.sector_median_anchor_yen, valuation.market_price_yen
    if anchor is None or price is None or price == 0:
        return None
    return round((anchor / price - 1) * 100, 4)


def cash_flow_from_analysis(analysis: Mapping[str, object]) -> dict[str, object]:
    """Keep source-bound amounts; document metadata is not a CF measurement period."""
    raw = analysis.get("metrics")
    metrics = raw if isinstance(raw, Mapping) else {}
    raw_quality = analysis.get("ttm_quality")
    quality = raw_quality if isinstance(raw_quality, Mapping) else {}
    return {
        "jquants": {
            "ocf_ttm": _number(metrics.get("ocf_ttm")),
            "ocf_yield": _number(metrics.get("ocf_yield")),
            "ttm_quality": _text(quality.get("ocf_yield")),
            "fin_latest_disclosed_date": _text(metrics.get("fin_latest_disclosed_date")),
        },
        "edinet": {
            "edinet_ocf_ttm": _number(metrics.get("edinet_ocf_ttm")),
            "capex_ttm": _number(metrics.get("capex_ttm")),
            "fcf_ttm": _number(metrics.get("fcf_ttm")),
            "fcf_yield": _number(metrics.get("fcf_yield")),
            "ttm_quality": _text(quality.get("fcf_yield")),
            "edinet_capex_source": _text(metrics.get("edinet_capex_source")),
            "document_metadata": {
                key: _text(metrics.get(key))
                for key in (
                    "edinet_source_doc_id",
                    "edinet_document_type",
                    "edinet_source_submit_datetime",
                    "edinet_source_period_start",
                    "edinet_source_period_end",
                )
            },
        },
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if isfinite(value) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None
