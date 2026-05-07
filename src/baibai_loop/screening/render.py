from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import date, timedelta, timezone
from pathlib import Path

import yaml

from .schema import (
    EvidenceHit,
    FreshnessWarning,
    ScreenedCandidate,
    ScreenedRunDocument,
    TTMQuality,
)

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
        Path("records/04-candidates")
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
    front_matter["requires_decision_coverage"] = True
    screening_rules_ref = "records/_config/screening-rules/2026-05-01T000000+0900.yaml"
    metric_catalog_ref = "records/_config/metric-catalog/2026-05-01T000000+0900.yaml"
    policy_ref = "records/01-policy/2026/05/2026-05-01T000000+0900-portfolio-policy.md"
    universe_ref = document.universe_snapshot_ref or _default_universe_snapshot_ref(document)
    front_matter["screening_rules_snapshot"] = {
        "ref_path": screening_rules_ref,
        "content_sha256": QuotedString(_content_sha256(screening_rules_ref, document.config_hash)),
    }
    front_matter["metric_catalog_snapshot"] = {
        "ref_path": metric_catalog_ref,
        "content_sha256": QuotedString(_content_sha256(metric_catalog_ref, document.config_hash)),
    }
    front_matter["policy_snapshot"] = {
        "ref_path": policy_ref,
        "content_sha256": QuotedString(_content_sha256(policy_ref, document.config_hash)),
    }
    front_matter["universe_snapshot_ref"] = {
        "ref_path": universe_ref,
        "content_sha256": QuotedString(_content_sha256(universe_ref, document.cache_manifest_hash)),
    }
    front_matter["cache_manifest_hash"] = QuotedString(document.cache_manifest_hash)
    front_matter["candidates"] = [
        _build_candidate_entry(candidate, document) for candidate in document.candidates
    ]
    front_matter["fact_memo_lines"] = [QuotedString(line) for line in document.fact_memo_lines]
    front_matter["provider_status_lines"] = [
        QuotedString(line) for line in document.provider_status_lines
    ]
    front_matter["universe_exclusion_lines"] = [
        QuotedString(line) for line in document.universe_exclusion_lines
    ]
    front_matter["ttm_quality_counts"] = dict(document.ttm_quality_counts)
    front_matter["evidence_hits_summary"] = dict(document.evidence_hits_summary)
    front_matter["fallback_lines"] = [QuotedString(line) for line in document.fallback_lines]
    return front_matter


def _build_candidate_entry(
    candidate: ScreenedCandidate, document: ScreenedRunDocument
) -> dict[str, object]:
    entry: dict[str, object] = {}
    entry["ticker"] = QuotedString(candidate.ticker)
    entry["name"] = QuotedString(candidate.name)
    candidate_id = f"candidate-{document.asof_date:%Y-%m-%d}-{candidate.ticker}"
    entry["screen_run_id"] = QuotedString(document.run_id)
    entry["candidate_id"] = QuotedString(candidate_id)
    entry["candidate_key"] = QuotedString(f"{document.run_id}:{candidate.ticker}")
    entry["playbook_screen_result"] = "hit"
    entry["policy_gate_result"] = "pass"
    entry["liquidity_gate_result"] = "pass"
    entry["macro_regime_gate_result"] = "pass"
    entry["per_forward"] = _round_value("per_forward", candidate.per_forward)
    entry["per_trailing"] = _round_value("per_trailing", candidate.per_trailing)
    entry["pbr"] = _round_value("pbr", candidate.pbr)
    entry["ev_ebitda"] = _round_value("ev_ebitda", candidate.ev_ebitda)
    entry["p_s"] = _round_value("p_s", candidate.p_s)
    entry["pcfr"] = _round_value("pcfr", candidate.pcfr)
    entry["sector_33"] = QuotedString(candidate.sector_33)
    entry["market_cap_oku"] = candidate.market_cap_oku
    entry["avg_turnover_oku"] = (
        round(candidate.avg_turnover_oku, 1) if candidate.avg_turnover_oku is not None else None
    )
    entry["price_change_60d"] = _round_ratio(candidate.price_change_60d)
    entry["price_change_4w"] = _round_ratio(candidate.price_change_4w)
    entry["sector_relative_strength_percentile"] = _round_ratio(
        candidate.sector_relative_strength_percentile
    )
    entry["metrics"] = _round_metrics(candidate.metrics)
    entry["metrics_breakdown"] = {
        metric: {
            "sector_median_gap": _round_ratio(values.get("sector_median_gap")),
            "self_range_percentile": _round_ratio(values.get("self_range_percentile")),
            "sigma_gap": _round_ratio(values.get("sigma_gap")),
        }
        for metric, values in candidate.metrics_breakdown.items()
    }
    entry["next_earnings_date"] = (
        QuotedString(candidate.next_earnings_date.isoformat())
        if candidate.next_earnings_date is not None
        else None
    )
    entry["split_adjustment_flag"] = candidate.split_adjustment_flag
    entry["freshness_warnings"] = [
        _build_freshness_warning(warning) for warning in candidate.freshness_warnings
    ]
    entry["ttm_quality"] = {
        "ev_ebitda": candidate.ttm_quality.get("ev_ebitda", TTMQuality.UNAVAILABLE).value,
        "p_s": candidate.ttm_quality.get("p_s", TTMQuality.UNAVAILABLE).value,
        "pcfr": candidate.ttm_quality.get("pcfr", TTMQuality.UNAVAILABLE).value,
        "ocf_yield": candidate.ttm_quality.get("ocf_yield", TTMQuality.UNAVAILABLE).value,
        "sales": candidate.ttm_quality.get("sales", TTMQuality.UNAVAILABLE).value,
        "fcf_yield": candidate.ttm_quality.get("fcf_yield", TTMQuality.UNAVAILABLE).value,
        "net_cash": candidate.ttm_quality.get("net_cash", TTMQuality.UNAVAILABLE).value,
    }
    entry["evidence_hits"] = [
        {
            "evidence_hit_id": QuotedString(f"{candidate_id}-{evidence_hit.name}"),
            "name": QuotedString(evidence_hit.name),
            "playbook_id": QuotedString(evidence_hit.playbook_id),
            "claim_id": QuotedString(f"{candidate.ticker}-{evidence_hit.name}"),
            "claim_type": QuotedString(evidence_hit.name),
            "evidence_family_set": _evidence_family_set(evidence_hit),
            "evidence_polarity": "supports",
            "decision_role": "sizing_evidence",
            "source_metric_ids": [QuotedString(key) for key in sorted(evidence_hit.metrics)],
            "source_status": "warning" if candidate.freshness_warnings else "ok",
            "sizing_eligible": not candidate.freshness_warnings,
            "freshness_dependency": "financial_statement",
            "independence_component_id": QuotedString(evidence_hit.name),
            "reasons": [QuotedString(reason) for reason in evidence_hit.reasons],
            "metrics": _round_metrics(evidence_hit.metrics),
        }
        for evidence_hit in candidate.evidence_hits
    ]
    return entry


def _content_sha256(ref_path: str, fallback_seed: str) -> str:
    path = Path(ref_path)
    if path.is_file():
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256(fallback_seed.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _evidence_family_set(evidence_hit: EvidenceHit) -> list[str]:
    metric_families = {
        "p_s": "valuation",
        "ps_sector_gap": "valuation",
        "sales_yoy": "fundamental",
        "operating_profit": "fundamental",
        "net_cash_to_market_cap": "fundamental",
        "cash_to_market_cap": "fundamental",
        "price_to_equity": "fundamental",
        "ocf_yield": "fundamental",
        "fcf_yield": "fundamental",
        "cfo_yoy": "fundamental",
        "sector_relative_strength_percentile": "market_derived",
        "price_change_60d": "market_derived",
    }
    families = {
        family
        for metric in evidence_hit.metrics
        for family in [metric_families.get(metric)]
        if family
    }
    if not families:
        families = {
            "valuation-reversion": {"valuation", "market_derived"},
            "strict-net-cash-discount": {"fundamental"},
            "cash-rich-asset-discount": {"fundamental"},
            "cashflow-yield-discount": {"fundamental"},
            "fcf-yield-discount": {"fundamental"},
            "sales-discount-growth": {"valuation", "fundamental"},
        }.get(evidence_hit.name, {"valuation"})
    return sorted(families)


def _default_universe_snapshot_ref(document: ScreenedRunDocument) -> str:
    directory = (
        Path("records/_universe-snapshots")
        / f"{document.asof_date:%Y}"
        / f"{document.asof_date:%m}"
    )
    prefix = f"{document.asof_date:%Y-%m-%d}T"
    if directory.is_dir():
        candidates = sorted(directory.glob(f"{prefix}*.yaml"))
        if candidates:
            return str(candidates[-1])
    offset = document.run_at.strftime("%z")
    return (
        f"records/_universe-snapshots/{document.asof_date:%Y/%m}/"
        f"{document.asof_date:%Y-%m-%d}T{document.run_at:%H%M%S}{offset}.yaml"
    )


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
