from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .run_store import RunPublication

import yaml

from baibai_engine.foundation.time import JST
from baibai_engine.market.metric_quality import TTMQuality

from .schema import FreshnessWarning, MetricValueMap, ScreeningRunDocument, StockAnalysis

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


def screening_run_payload(document: ScreeningRunDocument) -> dict[str, object]:
    # The document owns date validity; export one canonical display timezone.
    front_matter: dict[str, object] = {"schema_version": 1, "kind": "stock_analysis_run"}
    front_matter["run_date"] = QuotedString(document.run_date.isoformat())
    front_matter["asof_date"] = QuotedString(document.asof_date.isoformat())
    front_matter["universe_size"] = document.universe_size
    front_matter["filters"] = dict(document.filters.items())
    front_matter["generated_by"] = QuotedString(document.generated_by)
    front_matter["data_sources"] = [QuotedString(source) for source in document.data_sources]
    front_matter["run_at"] = QuotedString(document.run_at.astimezone(JST).isoformat())
    front_matter["run_id"] = QuotedString(document.run_id)
    front_matter["screening_rules_hash"] = QuotedString(document.screening_rules_hash)
    front_matter["er_model_version"] = QuotedString(document.er_model_version)
    front_matter["stock_analyses"] = [
        stock_analysis_payload(analysis) for analysis in document.stock_analyses
    ]
    front_matter["provider_status_lines"] = [
        QuotedString(line) for line in document.provider_status_lines
    ]
    front_matter["universe_exclusion_lines"] = [
        QuotedString(line) for line in document.universe_exclusion_lines
    ]
    front_matter["ttm_quality_counts"] = dict(document.ttm_quality_counts)
    front_matter["fallback_lines"] = [QuotedString(line) for line in document.fallback_lines]
    return front_matter


def stock_analysis_payload(analysis: StockAnalysis) -> dict[str, object]:
    """Serialize one analyzed security to its YAML entry shape.

    Production and calibration use the same rounded coordinates so a valuation
    approach cannot observe a different row in replay.
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
    entry["listing_span_days"] = analysis.listing_span_days
    entry["jpx_flags"] = [QuotedString(flag) for flag in analysis.jpx_flags]
    entry["price_change_1d"] = _round_ratio(analysis.price_change_1d)
    entry["price_change_5d"] = _round_ratio(analysis.price_change_5d)
    entry["price_change_20d"] = _round_ratio(analysis.price_change_20d)
    entry["price_change_60d"] = _round_ratio(analysis.price_change_60d)
    entry["gap_from_52w_low"] = _round_ratio(analysis.gap_from_52w_low)
    entry["turnover_spike_5d"] = _round_ratio(analysis.turnover_spike_5d)
    entry["sector_relative_strength_percentile"] = _round_ratio(
        analysis.sector_relative_strength_percentile
    )
    entry["price_history_sessions_750d"] = analysis.price_history_sessions_750d
    entry["price_history_coverage_750d"] = _round_ratio(analysis.price_history_coverage_750d)
    entry["metrics"] = _round_metrics(analysis.metrics)
    entry["next_earnings_date"] = (
        QuotedString(analysis.next_earnings_date.isoformat())
        if analysis.next_earnings_date is not None
        else None
    )
    entry["freshness_warnings"] = [
        _build_freshness_warning(warning) for warning in analysis.freshness_warnings
    ]
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


def _build_freshness_warning(warning: FreshnessWarning) -> dict[str, object]:
    return {
        "source_family": QuotedString(warning.source_family),
        "stale_metric": QuotedString(warning.stale_metric),
        "reason": QuotedString(warning.reason),
        "event_date": QuotedString(warning.event_date.isoformat()),
        "event_kind": QuotedString(warning.event_kind),
        "event_title": QuotedString(warning.event_title),
        "event_source": QuotedString(warning.event_source),
        "event_url": QuotedString(warning.event_url) if warning.event_url is not None else None,
        "edinet_source_submit_datetime": (
            QuotedString(warning.edinet_source_submit_datetime)
            if warning.edinet_source_submit_datetime is not None
            else None
        ),
    }


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
