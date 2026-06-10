"""Candidate / prior-research record types and their loaders."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from ._coerce import (
    _date_from_datetime_prefix,
    _dict_sequence,
    _mapping_sequence,
    _metric_map,
    _number,
    _parse_date,
    _string_value,
)


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    ticker: str
    name: str | None
    sector_33: str
    market_cap_oku: float | None
    avg_turnover_oku: float | None
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


@dataclass(frozen=True, slots=True)
class PriorResearch:
    ticker: str
    outcome: str | None
    posture: str | None
    reason_code: str | None
    deferral_reason: str | None
    revisit_after: date | None
    expires_at: date | None
    decision_event_at: str | None
    decision_event_id: str | None
    research_ref: str | None

    def suppression_reason(self, asof_date: date) -> str | None:
        if self.outcome == "deferred":
            if self.revisit_after is None:
                return "deferred_without_revisit_after"
            if self.revisit_after > asof_date:
                return "deferred_until_revisit_after"
            return None
        if self.outcome == "rejected" and (self.expires_at is None or self.expires_at > asof_date):
            return "prior_rejected"
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "posture": self.posture,
            "reason_code": self.reason_code,
            "deferral_reason": self.deferral_reason,
            "revisit_after": self.revisit_after.isoformat() if self.revisit_after else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "decision_event_at": self.decision_event_at,
            "decision_event_id": self.decision_event_id,
            "research_ref": self.research_ref,
        }


@dataclass(frozen=True, slots=True)
class PreviousCandidates:
    ref_path: str | None
    tickers: tuple[str, ...]


def candidate_record_from_mapping(raw: Mapping[str, object]) -> CandidateRecord:
    evidence_hits = tuple(_mapping_sequence(raw.get("evidence_hits")))
    freshness_warnings = tuple(_mapping_sequence(raw.get("freshness_warnings")))
    return CandidateRecord(
        ticker=str(raw.get("ticker") or ""),
        name=_string_value(raw.get("name")),
        sector_33=_string_value(raw.get("sector_33")) or "",
        market_cap_oku=_number(raw.get("market_cap_oku")),
        avg_turnover_oku=_number(raw.get("avg_turnover_oku")),
        evidence_hits=evidence_hits,
        metrics=_metric_map(raw.get("metrics")),
        freshness_warnings=freshness_warnings,
        next_earnings_date=_string_value(raw.get("next_earnings_date")),
        price_change_1d=_number(raw.get("price_change_1d")),
        price_change_5d=_number(raw.get("price_change_5d")),
        price_change_20d=_number(raw.get("price_change_20d")),
        price_change_60d=_number(raw.get("price_change_60d")),
        gap_from_52w_low=_number(raw.get("gap_from_52w_low")),
        turnover_spike_5d=_number(raw.get("turnover_spike_5d")),
    )


_EXPECTED_NUMERIC_METRICS = frozenset(
    {
        "cash_to_market_cap",
        "equity_ratio",
        "fcf_yield",
        "net_cash_to_market_cap",
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


def load_prior_research(ledger_root: Path, asof_date: date) -> dict[str, PriorResearch]:
    if not ledger_root.exists():
        return {}
    latest: dict[str, tuple[tuple[str, str], PriorResearch]] = {}
    for path in sorted(ledger_root.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if raw.get("decision_scope") != "research_memo":
                continue
            event_at = _string_value(raw.get("decision_event_at"))
            event_date = _date_from_datetime_prefix(event_at)
            if event_date is None or event_date > asof_date:
                continue
            ticker = _string_value(raw.get("ticker"))
            decision = raw.get("research_decision")
            if ticker is None or not isinstance(decision, Mapping):
                continue
            revisit = decision.get("revisit")
            revisit_map = revisit if isinstance(revisit, Mapping) else {}
            prior = PriorResearch(
                ticker=ticker,
                outcome=_string_value(decision.get("outcome")),
                posture=_string_value(decision.get("posture")),
                reason_code=_string_value(decision.get("reason_code")),
                deferral_reason=_string_value(decision.get("deferral_reason")),
                revisit_after=_parse_date(_string_value(revisit_map.get("revisit_after"))),
                expires_at=_parse_date(_string_value(revisit_map.get("expires_at"))),
                decision_event_at=event_at,
                decision_event_id=_string_value(raw.get("decision_event_id")),
                research_ref=_string_value(raw.get("research_ref")),
            )
            event_key = (event_at or "", prior.decision_event_id or "")
            previous = latest.get(ticker)
            if previous is None or event_key >= previous[0]:
                latest[ticker] = (event_key, prior)
    return {ticker: prior for ticker, (_, prior) in latest.items()}


def load_previous_candidates(
    candidates_root: Path,
    asof_date: date,
    *,
    current_path: Path | None = None,
) -> PreviousCandidates:
    if not candidates_root.exists():
        return PreviousCandidates(ref_path=None, tickers=())
    matches: list[tuple[date, int, str, Path]] = []
    for path in candidates_root.glob("*/*/*.yaml"):
        if current_path is not None and path.resolve() == current_path.resolve():
            continue
        parsed = _parse_date(path.stem)
        if parsed is not None and parsed < asof_date:
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                mtime_ns = 0
            matches.append((parsed, mtime_ns, path.as_posix(), path))
    if not matches:
        return PreviousCandidates(ref_path=None, tickers=())
    _, _, _, latest_path = sorted(matches)[-1]
    payload = yaml.safe_load(latest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        return PreviousCandidates(ref_path=latest_path.as_posix(), tickers=())
    tickers = tuple(
        ticker
        for item in _dict_sequence(payload.get("candidates"))
        if (ticker := _string_value(item.get("ticker"))) is not None
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
                evidence_name=_string_value(evidence_hit.get("name")),
                expected_metrics=_EXPECTED_NUMERIC_EVIDENCE_METRICS,
            )
        )
    return warnings
