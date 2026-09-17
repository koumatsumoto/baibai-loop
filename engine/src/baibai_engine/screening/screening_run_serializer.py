from __future__ import annotations

from collections.abc import Mapping

import yaml

from baibai_engine.foundation.time import JST

from .schema import (
    FreshnessWarning,
    ScreeningRunDocument,
    SecurityAnalysis,
    TTMQuality,
)

_DECIMAL_PLACES = {
    "per_forward": 2,
    "per_trailing": 2,
    "pbr": 2,
    "ev_ebitda": 1,
    "p_s": 2,
    "pcfr": 1,
}


class ScreeningRunSerializationError(ValueError):
    """Raised when a Screening Run YAML export cannot be serialized safely."""


class QuotedString(str):
    """A YAML string that should always be quoted."""


class _QuotedDumper(yaml.SafeDumper):
    pass


def _quoted_scalar_representer(dumper: yaml.SafeDumper, data: QuotedString) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')


_QuotedDumper.add_representer(QuotedString, _quoted_scalar_representer)


def serialize_screening_run_yaml(
    document: ScreeningRunDocument,
    *,
    run_revision_id: str | None = None,
) -> str:
    if document.run_date != document.asof_date:
        raise ScreeningRunSerializationError("run_date must equal asof_date")
    if document.run_at.tzinfo is None:
        raise ScreeningRunSerializationError("run_at must be timezone-aware")
    if document.run_at.utcoffset() != JST.utcoffset(None):
        raise ScreeningRunSerializationError("run_at must use JST (+09:00)")

    front_matter = _screening_run_payload(document)
    if run_revision_id is not None:
        front_matter["run_revision_id"] = QuotedString(run_revision_id)
    yaml_text = yaml.dump(
        front_matter,
        Dumper=_QuotedDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).strip()
    return f"{yaml_text}\n"


def _screening_run_payload(document: ScreeningRunDocument) -> dict[str, object]:
    front_matter: dict[str, object] = {}
    front_matter["run_date"] = QuotedString(document.run_date.isoformat())
    front_matter["asof_date"] = QuotedString(document.asof_date.isoformat())
    front_matter["universe_size"] = document.universe_size
    front_matter["filters"] = dict(document.filters.items())
    front_matter["generated_by"] = QuotedString(document.generated_by)
    front_matter["data_sources"] = [QuotedString(source) for source in document.data_sources]
    front_matter["run_at"] = QuotedString(document.run_at.isoformat())
    front_matter["run_id"] = QuotedString(document.run_id)
    front_matter["screening_rules_hash"] = QuotedString(document.screening_rules_hash)
    front_matter["er_model_version"] = QuotedString(document.er_model_version)
    front_matter["security_analyses"] = [
        security_analysis_payload(analysis) for analysis in document.security_analyses
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


def security_analysis_payload(analysis: SecurityAnalysis) -> dict[str, object]:
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
    entry["split_adjustment_flag"] = analysis.split_adjustment_flag
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


def _round_metrics(values: Mapping[str, object] | object) -> dict[str, object]:
    if not isinstance(values, Mapping):
        return {}
    rounded: dict[str, object] = {}
    for key, value in values.items():
        if isinstance(value, bool) or value is None or isinstance(value, str | int):
            rounded[key] = value
        elif isinstance(value, float):
            rounded[key] = _round_ratio(value)
    return rounded
