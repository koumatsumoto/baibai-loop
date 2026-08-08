"""Candidate / prior-research record types and their loaders."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from json import JSONDecodeError
from pathlib import Path
from typing import Literal

from baibai_engine.foundation.coerce import (
    mapping_sequence,
    metric_map,
    optional_float,
    parse_iso_date,
    string_or_none,
    string_sequence,
)

# Contract of the daily longlist records the serving export persists.
_LONGLIST_HISTORY_KIND = "daily-longlist-membership"
_LONGLIST_HISTORY_SCHEMA_VERSION = 1


class PreviousLonglistError(ValueError):
    """A persisted longlist record cannot be read as the previous candidate set."""


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


type PreviousCandidatesSource = Literal[
    "run_revision",
    "longlist_history",
    "canonical_shortlist",
]


@dataclass(frozen=True, slots=True)
class PreviousCandidates:
    """The earlier side of the new / continued / exited comparison.

    ``source`` states which population ``tickers`` came from because their sizes
    differ: a run revision carries every candidate of the prior as-of, a persisted
    daily longlist carries its top-N, and a canonical shortlist carries the retained
    entries reviewed by the human. The overlap ratio and previous-candidate cap read
    differently under each, so the selection reports the source instead of leaving
    the denominator implicit.
    """

    ref_path: str | None
    source: PreviousCandidatesSource | None
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


def load_previous_longlist(history_dir: Path, *, asof_date: date) -> PreviousCandidates:
    """Resolve the newest persisted longlist strictly before ``asof_date``.

    The run store keeps three generations, so repeating one as-of evicts the prior
    as-of and leaves the selection with no earlier side: every candidate then looks
    new and the previous-candidate cap stops binding. These records are written once
    per day and retained independently of that pruning, so they still name a
    predecessor when the store no longer does.

    A day whose record carries no longlist is skipped rather than treated as an
    empty predecessor, since "nobody was on the list" and "everything is new" are
    not the same statement. A record whose contract does not match raises, so a
    changed writer surfaces as a failure instead of as zero overlap.
    """
    if not history_dir.is_dir():
        return PreviousCandidates(ref_path=None, source=None, tickers=())
    dated: list[tuple[date, Path]] = []
    for path in history_dir.glob("*.json"):
        parsed = parse_iso_date(path.stem)
        if parsed is not None and parsed < asof_date:
            dated.append((parsed, path))
    for record_date, path in sorted(dated, reverse=True):
        tickers = _longlist_history_tickers(path, record_date=record_date)
        if tickers:
            return PreviousCandidates(
                ref_path=path.as_posix(),
                source="longlist_history",
                tickers=tickers,
            )
    return PreviousCandidates(ref_path=None, source=None, tickers=())


def _longlist_history_tickers(path: Path, *, record_date: date) -> tuple[str, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, JSONDecodeError) as exc:
        raise PreviousLonglistError(f"longlist history is unreadable: {path}") from exc
    if not isinstance(payload, Mapping):
        raise PreviousLonglistError(f"longlist history is not an object: {path}")
    if (
        payload.get("kind") != _LONGLIST_HISTORY_KIND
        or payload.get("schema_version") != _LONGLIST_HISTORY_SCHEMA_VERSION
    ):
        raise PreviousLonglistError(f"longlist history has an unsupported contract: {path}")
    if parse_iso_date(str(payload.get("as_of"))) != record_date:
        raise PreviousLonglistError(f"longlist history as-of does not match its name: {path}")
    members = payload.get("members")
    if not isinstance(members, list):
        raise PreviousLonglistError(f"longlist history members must be an array: {path}")
    tickers: list[str] = []
    for item in members:
        if not isinstance(item, Mapping):
            raise PreviousLonglistError(f"longlist history member is not an object: {path}")
        ticker = string_or_none(item.get("ticker"))
        if ticker is None:
            raise PreviousLonglistError(f"longlist history member has no ticker: {path}")
        tickers.append(ticker)
    return tuple(tickers)


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
