from __future__ import annotations

from datetime import date, timedelta, timezone
from pathlib import Path

import yaml

from .schema import ScreenedRunDocument, ScreenedTicker, TTMQuality

JST = timezone(timedelta(hours=9))
_DECIMAL_PLACES = {
    "per_forward": 2,
    "per_trailing": 2,
    "pbr": 2,
    "ev_ebitda": 1,
    "p_s": 2,
    "pcfr": 1,
}


class RenderError(ValueError):
    """Raised when screened YAML cannot be rendered safely."""


class QuotedString(str):
    """A YAML string that should always be quoted."""


class _QuotedDumper(yaml.SafeDumper):
    pass


def _quoted_scalar_representer(dumper: yaml.SafeDumper, data: QuotedString) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')


_QuotedDumper.add_representer(QuotedString, _quoted_scalar_representer)


def build_output_path(asof_date: date) -> Path:
    return (
        Path("records/03-candidates")
        / f"{asof_date:%Y}"
        / f"{asof_date:%m}"
        / f"{asof_date:%Y-%m-%d}.yaml"
    )


def render_screened_yaml(document: ScreenedRunDocument) -> str:
    if document.run_date != document.asof_date:
        raise RenderError("run_date must equal asof_date")
    if document.run_at.tzinfo is None:
        raise RenderError("run_at must be timezone-aware")
    if document.run_at.utcoffset() != JST.utcoffset(None):
        raise RenderError("run_at must use JST (+09:00)")

    front_matter = _build_front_matter(document)
    yaml_text = yaml.dump(
        front_matter,
        Dumper=_QuotedDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).strip()
    return f"{yaml_text}\n"


def _build_front_matter(document: ScreenedRunDocument) -> dict[str, object]:
    front_matter: dict[str, object] = {}
    front_matter["run_date"] = QuotedString(document.run_date.isoformat())
    front_matter["asof_date"] = QuotedString(document.asof_date.isoformat())
    front_matter["universe_size"] = document.universe_size
    front_matter["filters"] = dict(document.filters.items())
    front_matter["generated_by"] = QuotedString(document.generated_by)
    front_matter["data_sources"] = [QuotedString(source) for source in document.data_sources]
    front_matter["run_at"] = QuotedString(document.run_at.isoformat())
    front_matter["run_id"] = QuotedString(document.run_id)
    front_matter["config_hash"] = QuotedString(document.config_hash)
    front_matter["cache_manifest_hash"] = QuotedString(document.cache_manifest_hash)
    front_matter["tickers"] = [_build_ticker_entry(ticker) for ticker in document.tickers]
    front_matter["fact_memo_lines"] = [QuotedString(line) for line in document.fact_memo_lines]
    front_matter["provider_status_lines"] = [
        QuotedString(line) for line in document.provider_status_lines
    ]
    front_matter["universe_exclusion_lines"] = [
        QuotedString(line) for line in document.universe_exclusion_lines
    ]
    front_matter["ttm_quality_counts"] = dict(document.ttm_quality_counts)
    front_matter["fallback_lines"] = [QuotedString(line) for line in document.fallback_lines]
    return front_matter


def _build_ticker_entry(ticker: ScreenedTicker) -> dict[str, object]:
    entry: dict[str, object] = {}
    entry["ticker"] = QuotedString(ticker.ticker)
    entry["name"] = QuotedString(ticker.name)
    entry["per_forward"] = _round_value("per_forward", ticker.per_forward)
    entry["per_trailing"] = _round_value("per_trailing", ticker.per_trailing)
    entry["pbr"] = _round_value("pbr", ticker.pbr)
    entry["ev_ebitda"] = _round_value("ev_ebitda", ticker.ev_ebitda)
    entry["p_s"] = _round_value("p_s", ticker.p_s)
    entry["pcfr"] = _round_value("pcfr", ticker.pcfr)
    entry["sector_33"] = QuotedString(ticker.sector_33)
    entry["market_cap_oku"] = ticker.market_cap_oku
    entry["avg_turnover_oku"] = (
        round(ticker.avg_turnover_oku, 1) if ticker.avg_turnover_oku is not None else None
    )
    entry["price_change_60d"] = _round_ratio(ticker.price_change_60d)
    entry["price_change_4w"] = _round_ratio(ticker.price_change_4w)
    entry["sector_relative_strength_percentile"] = _round_ratio(
        ticker.sector_relative_strength_percentile
    )
    entry["metrics_breakdown"] = {
        metric: {
            "sector_median_gap": _round_ratio(values.get("sector_median_gap")),
            "self_range_percentile": _round_ratio(values.get("self_range_percentile")),
            "sigma_gap": _round_ratio(values.get("sigma_gap")),
        }
        for metric, values in ticker.metrics_breakdown.items()
    }
    entry["next_earnings_date"] = (
        QuotedString(ticker.next_earnings_date.isoformat())
        if ticker.next_earnings_date is not None
        else None
    )
    entry["corporate_action_flag"] = ticker.corporate_action_flag
    entry["ttm_quality"] = {
        "ev_ebitda": ticker.ttm_quality.get("ev_ebitda", TTMQuality.UNAVAILABLE).value,
        "p_s": ticker.ttm_quality.get("p_s", TTMQuality.UNAVAILABLE).value,
        "pcfr": ticker.ttm_quality.get("pcfr", TTMQuality.UNAVAILABLE).value,
    }
    entry["threshold_hit"] = list(ticker.threshold_hit)
    return entry


def _round_ratio(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def _round_value(metric: str, value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), _DECIMAL_PLACES[metric])
