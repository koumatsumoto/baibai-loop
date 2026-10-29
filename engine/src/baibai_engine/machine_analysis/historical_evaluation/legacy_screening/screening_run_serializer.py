from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.run_store import (
        RunPublication,
    )

import yaml

from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.schema import (
    MetricValueMap,
    StockAnalysis,
)
from baibai_engine.machine_analysis.observations.financials.metric_quality import TTMQuality

_DECIMAL_PLACES = {
    "per_forward": 2,
    "per_trailing": 2,
    "pbr": 2,
    "ev_ebitda": 1,
    "p_s": 2,
    "pcfr": 1,
}


class QuotedString(str):
    """A YAML string that should always be quoted."""


class _QuotedDumper(yaml.SafeDumper):
    pass


def _quoted_scalar_representer(dumper: yaml.SafeDumper, data: QuotedString) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')


_QuotedDumper.add_representer(QuotedString, _quoted_scalar_representer)


def published_run_payload(run: RunPublication) -> dict[str, object]:
    """Export the exact saved revision without another rounding or calculation pass."""
    return {
        **run.payload,
        "run_revision_id": run.run_revision_id,
        "stock_analyses": list(run.stock_analyses),
    }


def serialize_published_run_yaml(run: RunPublication) -> str:
    return (
        yaml.dump(
            _quoted_payload(published_run_payload(run)),
            Dumper=_QuotedDumper,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        ).strip()
        + "\n"
    )


def _quoted_payload(value: object) -> object:
    if isinstance(value, str):
        return QuotedString(value)
    if isinstance(value, Mapping):
        return {key: _quoted_payload(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_quoted_payload(item) for item in value]
    return value


def stock_analysis_payload(analysis: StockAnalysis) -> dict[str, object]:
    """Serialize one analyzed security to its YAML entry shape.

    calibrationで使う丸め済み座標を維持する。保存済みrunのexportは
    published_run_payloadでraw値を返し、この再計算用のshapeへ変換しない。
    新しい五モデルへこの丸めや旧候補契約を適用しない。
    """
    entry: dict[str, object] = {}
    entry["ticker"] = QuotedString(analysis.ticker)
    entry["name"] = QuotedString(analysis.name)
    entry["per_forward"] = _round_value("per_forward", analysis.per_forward)
    entry["per_trailing"] = _round_value("per_trailing", analysis.per_trailing)
    entry["pbr"] = _round_value("pbr", analysis.pbr)
    entry["ev_ebitda"] = _round_value("ev_ebitda", analysis.ev_ebitda)
    entry["p_s"] = _round_value("p_s", analysis.p_s)
    entry["pcfr"] = _round_value("pcfr", analysis.pcfr)
    entry["sector_33"] = QuotedString(analysis.sector_33)
    entry["market_cap_oku"] = analysis.market_cap_oku
    entry["avg_turnover_oku"] = (
        round(analysis.avg_turnover_oku, 1) if analysis.avg_turnover_oku is not None else None
    )
    entry["price_history_span_days"] = analysis.price_history_span_days
    entry["jpx_flags"] = [QuotedString(flag) for flag in analysis.jpx_flags]
    entry["price_change_60d"] = _round_ratio(analysis.price_change_60d)
    entry["gap_from_52w_low"] = _round_ratio(analysis.gap_from_52w_low)
    entry["price_history_sessions_750d"] = analysis.price_history_sessions_750d
    entry["price_history_coverage_750d"] = _round_ratio(analysis.price_history_coverage_750d)
    entry["metrics"] = _round_metrics(analysis.metrics)
    entry["ttm_quality"] = {
        "ev_ebitda": analysis.ttm_quality["ev_ebitda"].value
        if "ev_ebitda" in analysis.ttm_quality
        else None,
        "per_trailing": analysis.ttm_quality.get("per_trailing", TTMQuality.UNAVAILABLE).value,
        "p_s": analysis.ttm_quality.get("p_s", TTMQuality.UNAVAILABLE).value,
        "pcfr": analysis.ttm_quality.get("pcfr", TTMQuality.UNAVAILABLE).value,
        "ocf_yield": analysis.ttm_quality.get("ocf_yield", TTMQuality.UNAVAILABLE).value,
        "sales": analysis.ttm_quality.get("sales", TTMQuality.UNAVAILABLE).value,
        "fcf_yield": analysis.ttm_quality["fcf_yield"].value
        if "fcf_yield" in analysis.ttm_quality
        else None,
        "net_cash": analysis.ttm_quality.get("net_cash", TTMQuality.UNAVAILABLE).value,
    }
    entry["candidate"] = (
        analysis.candidate.model_dump(mode="json") if analysis.candidate is not None else None
    )
    return entry


def _round_ratio(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def _round_value(metric: str, value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), _DECIMAL_PLACES[metric])


def _round_metrics(values: MetricValueMap) -> dict[str, object]:
    return {
        key: _round_ratio(value) if isinstance(value, float) else value
        for key, value in values.items()
    }
