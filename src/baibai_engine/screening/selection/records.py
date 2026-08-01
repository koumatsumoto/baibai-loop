"""Candidate / prior-research record types and their loaders."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from baibai_engine.foundation.coerce import (
    dict_sequence,
    mapping_sequence,
    metric_map,
    optional_float,
    parse_iso_date,
    string_or_none,
    string_sequence,
)
from baibai_engine.foundation.yaml_io import safe_load


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    ticker: str
    name: str | None
    sector_33: str
    market_cap_oku: float | None
    avg_turnover_oku: float | None
    listing_span_days: float | None
    jpx_flags: tuple[str, ...] | None
    evidence_hits: tuple[Mapping[str, object], ...]
    metrics: Mapping[str, object]
    freshness_warnings: tuple[Mapping[str, object], ...]
    next_earnings_date: str | None
    price_change_1d: float | None = None
    price_change_5d: float | None = None
    price_change_20d: float | None = None
    price_change_60d: float | None = None
    gap_from_52w_low: float | None = None
    turnover_spike_5d: float | None = None
    price_history_coverage_750d: float | None = None
    split_adjustment_flag: bool = False
    # Top-level valuation multiples from the screen output. The selection
    # ranking reads cheapness from evidence-hit metrics; the scorecard triage
    # surfaces these headline multiples as a valuation-discount coordinate.
    per_trailing: float | None = None
    per_forward: float | None = None
    pbr: float | None = None
    p_s: float | None = None
    ev_ebitda: float | None = None
    pcfr: float | None = None


@dataclass(frozen=True, slots=True)
class PreviousCandidates:
    ref_path: str | None
    tickers: tuple[str, ...]


def candidate_record_from_mapping(raw: Mapping[str, object]) -> CandidateRecord:
    evidence_hits = tuple(mapping_sequence(raw.get("evidence_hits")))
    freshness_warnings = tuple(mapping_sequence(raw.get("freshness_warnings")))
    return CandidateRecord(
        ticker=str(raw.get("ticker") or ""),
        name=string_or_none(raw.get("name")),
        sector_33=string_or_none(raw.get("sector_33")) or "",
        market_cap_oku=optional_float(raw.get("market_cap_oku")),
        avg_turnover_oku=optional_float(raw.get("avg_turnover_oku")),
        listing_span_days=optional_float(raw.get("listing_span_days")),
        jpx_flags=(string_sequence(raw["jpx_flags"]) if "jpx_flags" in raw else None),
        evidence_hits=evidence_hits,
        metrics=metric_map(raw.get("metrics")),
        freshness_warnings=freshness_warnings,
        next_earnings_date=string_or_none(raw.get("next_earnings_date")),
        price_change_1d=optional_float(raw.get("price_change_1d")),
        price_change_5d=optional_float(raw.get("price_change_5d")),
        price_change_20d=optional_float(raw.get("price_change_20d")),
        price_change_60d=optional_float(raw.get("price_change_60d")),
        gap_from_52w_low=optional_float(raw.get("gap_from_52w_low")),
        turnover_spike_5d=optional_float(raw.get("turnover_spike_5d")),
        price_history_coverage_750d=optional_float(raw.get("price_history_coverage_750d")),
        split_adjustment_flag=raw.get("split_adjustment_flag") is True,
        per_trailing=optional_float(raw.get("per_trailing")),
        per_forward=optional_float(raw.get("per_forward")),
        pbr=optional_float(raw.get("pbr")),
        p_s=optional_float(raw.get("p_s")),
        ev_ebitda=optional_float(raw.get("ev_ebitda")),
        pcfr=optional_float(raw.get("pcfr")),
    )


_EXPECTED_NUMERIC_METRICS = frozenset(
    {
        "cash_to_market_cap",
        "asset_backed_ratio",
        "equity_ratio",
        "fcf_yield",
        "net_cash_to_market_cap",
        "investment_securities",
        "ocf_yield",
        "operating_profit",
        "price_to_equity",
        "sales_yoy",
    }
)

_EXPECTED_NUMERIC_EVIDENCE_METRICS = _EXPECTED_NUMERIC_METRICS | frozenset(
    {
        "cfo_yoy",
        "condition_a_sector_median_gap",
        "condition_a_self_range_percentile",
        "condition_b_sigma_gap",
        "fcf_margin",
        "operating_margin",
        "price_change_60d",
        "ps_sector_gap",
    }
)


def load_previous_candidates(
    candidates_root: Path,
    asof_date: date,
    *,
    current_path: Path | None = None,
    payload_cache: dict[Path, Mapping[str, object]] | None = None,
) -> PreviousCandidates:
    """Resolve the most recent candidates YAML before ``asof_date``.

    ``payload_cache`` lets the caller share parsed payloads with
    ``load_week_candidates`` so the same YAML is not loaded twice when this
    function is invoked once per week alongside the per-week sweep.
    """
    if not candidates_root.exists():
        return PreviousCandidates(ref_path=None, tickers=())
    matches: list[tuple[date, int, str, Path]] = []
    for path in candidates_root.glob("*/*/*.yaml"):
        if current_path is not None and path.resolve() == current_path.resolve():
            continue
        parsed = parse_iso_date(path.stem)
        if parsed is not None and parsed < asof_date:
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                mtime_ns = 0
            matches.append((parsed, mtime_ns, path.as_posix(), path))
    if not matches:
        return PreviousCandidates(ref_path=None, tickers=())
    _, _, _, latest_path = sorted(matches)[-1]
    if payload_cache is not None:
        cache_key = latest_path.resolve()
        cached = payload_cache.get(cache_key)
        if cached is not None:
            payload: object = cached
        else:
            payload = safe_load(latest_path.read_text(encoding="utf-8"))
            if isinstance(payload, Mapping):
                payload_cache[cache_key] = payload
    else:
        payload = safe_load(latest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        return PreviousCandidates(ref_path=latest_path.as_posix(), tickers=())
    tickers = tuple(
        ticker
        for item in dict_sequence(payload.get("candidates"))
        if (ticker := string_or_none(item.get("ticker"))) is not None
    )
    return PreviousCandidates(ref_path=latest_path.as_posix(), tickers=tickers)


def _numeric_metric_type_warnings(
    metrics: Mapping[str, object],
    *,
    source: str,
    evidence_name: str | None = None,
    expected_metrics: frozenset[str] = _EXPECTED_NUMERIC_METRICS,
) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    for key in sorted(expected_metrics):
        value = metrics.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            warning: dict[str, object] = {
                "source": source,
                "metric": key,
                "value_type": type(value).__name__,
            }
            if evidence_name is not None:
                warning["evidence_name"] = evidence_name
            warnings.append(warning)
    return warnings


def _evidence_metric_type_warnings(
    evidence_hits: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    for evidence_hit in evidence_hits:
        metrics = evidence_hit.get("metrics")
        if not isinstance(metrics, Mapping):
            continue
        warnings.extend(
            _numeric_metric_type_warnings(
                metrics,
                source="evidence_hits.metrics",
                evidence_name=string_or_none(evidence_hit.get("name")),
                expected_metrics=_EXPECTED_NUMERIC_EVIDENCE_METRICS,
            )
        )
    return warnings
