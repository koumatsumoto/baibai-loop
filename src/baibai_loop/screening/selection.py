from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

from .rule_config import ScreeningRules, SelectionDiversityRules, SelectionRules
from .tiers import position_tier

SUPPORTED_OUTLOOK_STATUSES = {"supportive", "neutral"}

_LANE_RANK = {
    "valuation-reversion": 0,
    "strict-net-cash-discount": 1,
    "fcf-yield-discount": 2,
    "cash-rich-asset-discount": 3,
    "cashflow-yield-discount": 4,
    "sales-discount-growth": 5,
}

BUILTIN_PROFILE_OVERRIDES: Mapping[str, Mapping[str, object]] = {
    "strict": {
        "fast_dislocation": {
            "price_change_1d_max": -0.07,
            "price_change_5d_max": -0.12,
            "price_change_20d_max": -0.15,
            "price_change_60d_max": -0.25,
            "gap_from_52w_low_max": 0.10,
            "turnover_spike_5d_min": 2.0,
            "min_fundamental_guard_count": 2,
            "high_confidence_guard_count": 3,
            "ocf_yield_min": 0.10,
            "fcf_yield_min": 0.07,
            "equity_ratio_min": 0.45,
            "price_to_equity_max": 1.0,
            "net_cash_to_market_cap_min": 0.25,
        },
        "diversity": {
            "max_recommended_per_sector": 1,
            "max_recommended_per_lane": 1,
            "max_previous_candidates_in_recommended": 2,
        },
    },
    "balanced": {},
    "loose": {
        "fast_dislocation": {
            "price_change_1d_max": -0.03,
            "price_change_5d_max": -0.05,
            "price_change_20d_max": -0.08,
            "price_change_60d_max": -0.12,
            "gap_from_52w_low_max": 0.25,
            "turnover_spike_5d_min": None,
            "min_fundamental_guard_count": 1,
            "high_confidence_guard_count": 2,
            "ocf_yield_min": 0.05,
            "fcf_yield_min": 0.03,
            "equity_ratio_min": 0.35,
            "price_to_equity_max": 1.1,
            "net_cash_to_market_cap_min": 0.10,
        },
        "diversity": {
            "max_recommended_per_sector": 3,
            "max_recommended_per_lane": 3,
            "max_previous_candidates_in_recommended": 4,
        },
    },
}


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    ticker: str
    name: str | None
    sector_33: str
    market_cap_oku: float | None
    avg_turnover_oku: float | None
    evidence_hits: tuple[Mapping[str, object], ...]
    metrics: Mapping[str, object]
    metrics_breakdown: Mapping[str, object]
    freshness_warnings: tuple[Mapping[str, object], ...]
    next_earnings_date: str | None
    price_change_1d: float | None = None
    price_change_5d: float | None = None
    price_change_20d: float | None = None
    price_change_60d: float | None = None
    price_change_4w: float | None = None
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
        if self.outcome == "deferred" and self.revisit_after is not None:
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
        metrics_breakdown=_metric_map(raw.get("metrics_breakdown")),
        freshness_warnings=freshness_warnings,
        next_earnings_date=_string_value(raw.get("next_earnings_date")),
        price_change_1d=_number(raw.get("price_change_1d")),
        price_change_5d=_number(raw.get("price_change_5d")),
        price_change_20d=_number(raw.get("price_change_20d")),
        price_change_60d=_number(raw.get("price_change_60d")),
        price_change_4w=_number(raw.get("price_change_4w")),
        gap_from_52w_low=_number(raw.get("gap_from_52w_low")),
        turnover_spike_5d=_number(raw.get("turnover_spike_5d")),
    )


def load_profile_overrides(path: Path | None) -> dict[str, Mapping[str, object]]:
    if path is None:
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"profile YAML root must be a mapping: {path}")
    raw_profiles = payload.get("profiles", payload)
    if not isinstance(raw_profiles, Mapping):
        raise ValueError(f"profile YAML must contain a profiles mapping: {path}")
    profiles: dict[str, Mapping[str, object]] = {}
    for name, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, Mapping):
            raise ValueError(f"profile {name!r} must be a mapping: {path}")
        profiles[str(name)] = raw_profile
    return profiles


def resolve_selection_rules(
    base: SelectionRules,
    *,
    profile: str,
    profile_overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> SelectionRules:
    data = base.model_dump(mode="python")
    overrides = BUILTIN_PROFILE_OVERRIDES.get(profile)
    if overrides is None:
        overrides = (profile_overrides or {}).get(profile)
    elif profile_overrides and profile in profile_overrides:
        overrides = _deep_merge(overrides, profile_overrides[profile])
    if overrides is None:
        raise ValueError(f"unknown selection profile: {profile}")
    if overrides is not None:
        data = _deep_merge(data, overrides)
    data["default_profile"] = profile
    return SelectionRules.model_validate(data)


def build_selection_payload(
    *,
    asof_date: date,
    candidates: Sequence[CandidateRecord],
    sectors_outlook: Mapping[str, str | None],
    rules: ScreeningRules,
    top: int,
    profile: str | None,
    candidates_ref: str,
    outlook_ref: str,
    previous_candidates: PreviousCandidates | None = None,
    prior_research_by_ticker: Mapping[str, PriorResearch] | None = None,
    profile_overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    effective_profile = profile or rules.selection.default_profile
    selection_rules = resolve_selection_rules(
        rules.selection,
        profile=effective_profile,
        profile_overrides=profile_overrides,
    )
    recommendation_limit = _research_recommendation_limit(
        top=top,
        configured_max=rules.output.research_selection_target_max,
    )
    prior_research_by_ticker = prior_research_by_ticker or {}
    previous_candidates = previous_candidates or PreviousCandidates(ref_path=None, tickers=())
    previous_tickers = set(previous_candidates.tickers)

    ranked_entries: list[tuple[tuple[object, ...], dict[str, object]]] = []
    lane_toplist_entries: dict[str, list[tuple[tuple[object, ...], dict[str, object]]]] = {
        lane: [] for lane in rules.lane_order
    }
    suppressed_queue: list[dict[str, object]] = []

    for item in candidates:
        outlook_status = sectors_outlook.get(item.sector_33)
        if outlook_status not in SUPPORTED_OUTLOOK_STATUSES:
            continue
        eligible_evidence_hits = _sizing_eligible_evidence_hits(item.evidence_hits)
        if not eligible_evidence_hits:
            continue
        selection_lane, selection_metrics, strength_key = _best_selection_evidence(
            eligible_evidence_hits
        )
        prior_research = prior_research_by_ticker.get(item.ticker)
        suppress_reason = (
            prior_research.suppression_reason(asof_date) if prior_research is not None else None
        )
        lenses = _candidate_lenses(item, selection_rules)
        candidate = _selection_candidate(
            item,
            outlook_status=outlook_status,
            selection_lane=selection_lane,
            selection_metrics=selection_metrics,
            recommendation_lane=None,
            lenses=lenses,
            prior_research=prior_research,
            suppressed=suppress_reason is not None,
            suppression_reasons=(suppress_reason,) if suppress_reason else (),
            previous_candidate=item.ticker in previous_tickers,
        )
        sort_key = (
            _macro_rank(outlook_status),
            _lane_rank(selection_lane),
            *strength_key,
            -len(eligible_evidence_hits),
            item.ticker,
        )
        ranked_entries.append((sort_key, candidate))
        if suppress_reason is not None:
            suppressed_queue.append(candidate)
        for evidence_hit in eligible_evidence_hits:
            lane_name = _string_value(evidence_hit.get("name"))
            if lane_name not in lane_toplist_entries:
                continue
            metrics = _metric_map(evidence_hit.get("metrics"))
            lane_sort_key = (
                _macro_rank(outlook_status),
                *_evidence_strength_key(lane_name, metrics),
                -len(eligible_evidence_hits),
                item.ticker,
            )
            lane_toplist_entries[lane_name].append(
                (
                    lane_sort_key,
                    _selection_candidate(
                        item,
                        outlook_status=outlook_status,
                        selection_lane=lane_name,
                        selection_metrics=metrics,
                        recommendation_lane=lane_name,
                        lenses=lenses,
                        prior_research=prior_research,
                        suppressed=suppress_reason is not None,
                        suppression_reasons=(suppress_reason,) if suppress_reason else (),
                        previous_candidate=item.ticker in previous_tickers,
                    ),
                )
            )

    ranked_entries.sort(key=lambda item: item[0])
    ranked_candidates = [candidate for _, candidate in ranked_entries]
    deferred_revisit_queue = _deferred_revisit_queue(ranked_candidates)
    lane_toplists = _rank_lane_toplists(lane_toplist_entries, rules.output.lane_toplist_limit)
    ranked_active_candidates = [
        candidate for candidate in ranked_candidates if not candidate["suppressed"]
    ]
    core_value_queue = _core_value_queue(
        lane_toplists=lane_toplists,
        ranked_candidates=ranked_active_candidates,
        lane_order=rules.output.research_selection_lane_order,
    )
    fast_dislocation_queue = _fast_dislocation_queue(ranked_active_candidates)
    long_hold_queue = _long_hold_survivability_queue(ranked_active_candidates)
    queue_map = {
        "fast_dislocation_queue": fast_dislocation_queue,
        "core_value_queue": core_value_queue,
        "long_hold_survivability_queue": long_hold_queue,
    }
    recommended = _recommended_research_candidates(
        queue_map=queue_map,
        queue_order=selection_rules.queue_order,
        ranked_candidates=core_value_queue,
        lane_order=rules.output.research_selection_lane_order,
        diversity_rules=selection_rules.diversity,
        min_count=rules.output.research_selection_target_min,
        limit=recommendation_limit,
    )
    diagnostics = _diagnostics(
        recommended=recommended,
        ranked_candidates=ranked_candidates,
        previous_candidates=previous_candidates,
        diversity_warning_ratio=selection_rules.diversity.previous_overlap_warning_ratio,
        profile=effective_profile,
    )
    return {
        "asof": asof_date.isoformat(),
        "candidates_ref": candidates_ref,
        "outlook_ref": outlook_ref,
        "input_count": len(candidates),
        "after_outlook_filter": len(ranked_candidates),
        "selection_mode": "queues",
        "research_selection_target_min": rules.output.research_selection_target_min,
        "research_selection_target_max": rules.output.research_selection_target_max,
        "research_selection_lane_order": list(rules.output.research_selection_lane_order),
        "lane_toplist_limit": rules.output.lane_toplist_limit,
        "selection": {
            "asof": asof_date.isoformat(),
            "profile": effective_profile,
            "input_refs": {
                "candidates_ref": candidates_ref,
                "outlook_ref": outlook_ref,
                "previous_candidates_ref": previous_candidates.ref_path,
            },
            "input_count": len(candidates),
            "after_outlook_filter": len(ranked_candidates),
            "selection_mode": "queues",
            "research_selection_target_min": rules.output.research_selection_target_min,
            "research_selection_target_max": rules.output.research_selection_target_max,
            "research_selection_lane_order": list(rules.output.research_selection_lane_order),
            "lane_toplist_limit": rules.output.lane_toplist_limit,
            "diagnostics": diagnostics,
        },
        "queues": {
            "recommended_research_queue": recommended,
            "core_value_queue": core_value_queue[:top],
            "fast_dislocation_queue": fast_dislocation_queue[:top],
            "long_hold_survivability_queue": long_hold_queue[:top],
            "deferred_revisit_queue": deferred_revisit_queue[:top],
            "suppressed_queue": suppressed_queue[:top],
        },
        "lane_toplists": lane_toplists,
        "ranked_candidates": ranked_candidates[:top],
        # Legacy alias kept for existing CLI users; docs make queues canonical.
        "candidates": recommended,
    }


def build_selection_sweep_payload(
    *,
    asof_date: date,
    candidates: Sequence[CandidateRecord],
    sectors_outlook: Mapping[str, str | None],
    rules: ScreeningRules,
    top: int,
    profiles: Sequence[str],
    candidates_ref: str,
    outlook_ref: str,
    previous_candidates: PreviousCandidates | None = None,
    prior_research_by_ticker: Mapping[str, PriorResearch] | None = None,
    profile_overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    profile_results: list[dict[str, object]] = []
    for profile in profiles:
        payload = build_selection_payload(
            asof_date=asof_date,
            candidates=candidates,
            sectors_outlook=sectors_outlook,
            rules=rules,
            top=top,
            profile=profile,
            candidates_ref=candidates_ref,
            outlook_ref=outlook_ref,
            previous_candidates=previous_candidates,
            prior_research_by_ticker=prior_research_by_ticker,
            profile_overrides=profile_overrides,
        )
        selection = _mapping(payload.get("selection"))
        queues = _mapping(payload.get("queues"))
        recommended = _dict_sequence(queues.get("recommended_research_queue"))
        fast = _dict_sequence(queues.get("fast_dislocation_queue"))
        long_hold = _dict_sequence(queues.get("long_hold_survivability_queue"))
        suppressed = _dict_sequence(queues.get("suppressed_queue"))
        diagnostics = _mapping(selection.get("diagnostics"))
        profile_results.append(
            {
                "profile": profile,
                "recommended_tickers": [_string_value(item.get("ticker")) for item in recommended],
                "recommended_count": len(recommended),
                "fast_dislocation_tickers": [_string_value(item.get("ticker")) for item in fast],
                "fast_dislocation_count": len(fast),
                "long_hold_counts": _long_hold_counts(long_hold),
                "suppressed_count": len(suppressed),
                "previous_overlap": diagnostics.get("previous_overlap"),
                "concentration": diagnostics.get("concentration"),
                "warnings": diagnostics.get("warnings"),
            }
        )
    return {
        "asof": asof_date.isoformat(),
        "input_refs": {
            "candidates_ref": candidates_ref,
            "outlook_ref": outlook_ref,
            "previous_candidates_ref": previous_candidates.ref_path
            if previous_candidates is not None
            else None,
        },
        "profiles": profile_results,
    }


def load_prior_research(ledger_root: Path, asof_date: date) -> dict[str, PriorResearch]:
    if not ledger_root.exists():
        return {}
    latest: dict[str, tuple[str, PriorResearch]] = {}
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
            previous = latest.get(ticker)
            if previous is None or (event_at or "") >= previous[0]:
                latest[ticker] = (event_at or "", prior)
    return {ticker: prior for ticker, (_, prior) in latest.items()}


def load_previous_candidates(
    candidates_root: Path,
    asof_date: date,
    *,
    current_path: Path | None = None,
) -> PreviousCandidates:
    if not candidates_root.exists():
        return PreviousCandidates(ref_path=None, tickers=())
    matches: list[tuple[date, Path]] = []
    for path in candidates_root.glob("*/*/*.yaml"):
        if current_path is not None and path.resolve() == current_path.resolve():
            continue
        parsed = _parse_date(path.stem)
        if parsed is not None and parsed < asof_date:
            matches.append((parsed, path))
    if not matches:
        return PreviousCandidates(ref_path=None, tickers=())
    _, latest_path = sorted(matches)[-1]
    payload = yaml.safe_load(latest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        return PreviousCandidates(ref_path=latest_path.as_posix(), tickers=())
    tickers = tuple(
        ticker
        for item in _dict_sequence(payload.get("candidates"))
        if (ticker := _string_value(item.get("ticker"))) is not None
    )
    return PreviousCandidates(ref_path=latest_path.as_posix(), tickers=tickers)


def _candidate_lenses(item: CandidateRecord, rules: SelectionRules) -> dict[str, object]:
    return {
        "fast_dislocation": _fast_dislocation_lens(item, rules),
        "long_hold_survivability": _long_hold_survivability_lens(item, rules),
        "shareholder_return": {
            "rating": "unknown",
            "data_status": "missing_candidate_metrics",
            "reasons": [],
        },
        "ai_exposure": {
            "tags": list(rules.ai_exposure_sector_tags.get(item.sector_33, ())),
            "data_status": "sector_proxy",
        },
    }


def _fast_dislocation_lens(item: CandidateRecord, rules: SelectionRules) -> dict[str, object]:
    lane_rules = rules.fast_dislocation
    price_triggers: list[dict[str, object]] = []
    trigger_specs = (
        ("price_change_1d", item.price_change_1d, lane_rules.price_change_1d_max),
        ("price_change_5d", item.price_change_5d, lane_rules.price_change_5d_max),
        (
            "price_change_20d",
            item.price_change_20d if item.price_change_20d is not None else item.price_change_4w,
            lane_rules.price_change_20d_max,
        ),
        ("price_change_60d", item.price_change_60d, lane_rules.price_change_60d_max),
    )
    for metric, value, threshold in trigger_specs:
        if threshold is not None and value is not None and value <= threshold:
            price_triggers.append({"metric": metric, "value": value, "threshold": threshold})
    if (
        lane_rules.gap_from_52w_low_max is not None
        and item.gap_from_52w_low is not None
        and item.gap_from_52w_low <= lane_rules.gap_from_52w_low_max
    ):
        price_triggers.append(
            {
                "metric": "gap_from_52w_low",
                "value": item.gap_from_52w_low,
                "threshold": lane_rules.gap_from_52w_low_max,
            }
        )
    if (
        lane_rules.turnover_spike_5d_min is not None
        and item.turnover_spike_5d is not None
        and item.turnover_spike_5d >= lane_rules.turnover_spike_5d_min
    ):
        price_triggers.append(
            {
                "metric": "turnover_spike_5d",
                "value": item.turnover_spike_5d,
                "threshold": lane_rules.turnover_spike_5d_min,
            }
        )

    guard_reasons = _fundamental_guard_reasons(item, rules)
    guard_count = len(guard_reasons)
    eligible = (
        lane_rules.enabled
        and bool(price_triggers)
        and guard_count >= lane_rules.min_fundamental_guard_count
    )
    confidence = "none"
    if eligible:
        confidence = "high" if guard_count >= lane_rules.high_confidence_guard_count else "medium"
    return {
        "eligible": eligible,
        "confidence": confidence,
        "price_triggers": price_triggers,
        "fundamental_guard_count": guard_count,
        "fundamental_guard_reasons": guard_reasons,
        "data_status": _fast_dislocation_data_status(item),
    }


def _fundamental_guard_reasons(item: CandidateRecord, rules: SelectionRules) -> list[str]:
    lane_rules = rules.fast_dislocation
    metrics = item.metrics
    reasons: list[str] = []
    if _float_or(metrics.get("ocf_yield"), -1.0) >= lane_rules.ocf_yield_min:
        reasons.append("ocf_yield")
    if _float_or(metrics.get("fcf_yield"), -1.0) >= lane_rules.fcf_yield_min:
        reasons.append("fcf_yield")
    if (
        _float_or(metrics.get("price_to_equity"), 99.0) <= lane_rules.price_to_equity_max
        and _float_or(metrics.get("equity_ratio"), -1.0) >= lane_rules.equity_ratio_min
    ):
        reasons.append("asset_discount_with_equity_buffer")
    if _float_or(metrics.get("net_cash_to_market_cap"), -99.0) >= (
        lane_rules.net_cash_to_market_cap_min
    ):
        reasons.append("net_cash_buffer")
    operating_profit_ok = not lane_rules.operating_profit_positive_required or (
        _float_or(metrics.get("operating_profit"), -1.0) > 0
    )
    if (
        _float_or(metrics.get("sales_yoy"), -99.0) >= lane_rules.sales_yoy_min
        and operating_profit_ok
    ):
        reasons.append("sales_growth_with_profit")
    return reasons


def _fast_dislocation_data_status(item: CandidateRecord) -> str:
    if item.price_change_5d is None and item.price_change_20d is None:
        if item.price_change_4w is not None or item.price_change_60d is not None:
            return "fallback_4w_or_60d"
        return "missing_price_history"
    return "ok"


def _long_hold_survivability_lens(
    item: CandidateRecord,
    rules: SelectionRules,
) -> dict[str, object]:
    lens_rules = rules.long_hold_survivability
    metrics = item.metrics
    reasons: list[str] = []
    caution_reasons: list[str] = []
    equity_ratio = _number(metrics.get("equity_ratio"))
    if equity_ratio is None:
        caution_reasons.append("equity_ratio_missing")
    elif equity_ratio >= lens_rules.equity_ratio_high_min:
        reasons.append("high_equity_ratio")
    elif equity_ratio >= lens_rules.equity_ratio_medium_min:
        reasons.append("medium_equity_ratio")
    else:
        caution_reasons.append("low_equity_ratio")

    net_cash_to_market_cap = _number(metrics.get("net_cash_to_market_cap"))
    if net_cash_to_market_cap is not None and (
        net_cash_to_market_cap >= lens_rules.net_cash_to_market_cap_high_min
    ):
        reasons.append("net_cash_buffer")
    elif net_cash_to_market_cap is not None and (
        net_cash_to_market_cap >= lens_rules.net_cash_to_market_cap_medium_min
    ):
        reasons.append("non_negative_net_cash")

    if _float_or(metrics.get("cash_to_market_cap"), -1.0) >= (
        lens_rules.cash_to_market_cap_high_min
    ):
        reasons.append("cash_buffer")
    if _float_or(metrics.get("ocf_yield"), -1.0) > lens_rules.ocf_yield_positive_min:
        reasons.append("positive_ocf_yield")
    else:
        caution_reasons.append("weak_or_missing_ocf_yield")
    if _float_or(metrics.get("fcf_yield"), -1.0) > lens_rules.fcf_yield_positive_min:
        reasons.append("positive_fcf_yield")
    if _float_or(metrics.get("operating_profit"), -1.0) > 0:
        reasons.append("positive_operating_profit")
    else:
        caution_reasons.append("operating_profit_not_positive_or_missing")
    if (
        item.avg_turnover_oku is not None
        and item.avg_turnover_oku >= lens_rules.min_avg_turnover_oku
    ):
        reasons.append("liquidity_pass")
    else:
        caution_reasons.append("liquidity_low_or_missing")

    support_count = len(set(reasons))
    if support_count >= lens_rules.high_min_support_count and len(caution_reasons) <= 1:
        rating = "high"
    elif support_count >= lens_rules.medium_min_support_count:
        rating = "medium"
    elif support_count == 0 and len(caution_reasons) >= 3:
        rating = "unknown"
    else:
        rating = "low"
    return {
        "rating": rating,
        "support_count": support_count,
        "reasons": sorted(set(reasons)),
        "caution_reasons": sorted(set(caution_reasons)),
    }


def _selection_candidate(
    item: CandidateRecord,
    *,
    outlook_status: str | None,
    selection_lane: str | None,
    selection_metrics: Mapping[str, object],
    recommendation_lane: str | None,
    lenses: Mapping[str, object],
    prior_research: PriorResearch | None,
    suppressed: bool,
    suppression_reasons: Sequence[str],
    previous_candidate: bool,
) -> dict[str, object]:
    return {
        "ticker": item.ticker,
        "name": item.name,
        "sector_33": item.sector_33,
        "outlook_sector": outlook_status,
        "market_cap_oku": item.market_cap_oku,
        "price_change_1d": item.price_change_1d,
        "price_change_5d": item.price_change_5d,
        "price_change_20d": item.price_change_20d,
        "price_change_60d": item.price_change_60d,
        "price_change_4w": item.price_change_4w,
        "gap_from_52w_low": item.gap_from_52w_low,
        "turnover_spike_5d": item.turnover_spike_5d,
        "evidence_hits": list(item.evidence_hits),
        "independent_evidence_count": len(_sizing_eligible_evidence_hits(item.evidence_hits)),
        "freshness_warnings": list(item.freshness_warnings),
        "selection_lane": selection_lane,
        "recommendation_lane": recommendation_lane,
        "selection_metrics": dict(selection_metrics),
        "next_earnings_date": item.next_earnings_date,
        "position_tier": position_tier(item.market_cap_oku),
        "lenses": dict(lenses),
        "prior_research": prior_research.to_dict() if prior_research is not None else None,
        "previous_candidate": previous_candidate,
        "suppressed": suppressed,
        "suppression_reasons": list(suppression_reasons),
    }


def _rank_lane_toplists(
    lane_entries: Mapping[str, list[tuple[tuple[object, ...], dict[str, object]]]],
    limit: int,
) -> dict[str, list[dict[str, object]]]:
    output: dict[str, list[dict[str, object]]] = {}
    for lane, entries in lane_entries.items():
        entries.sort(key=lambda item: item[0])
        output[lane] = [candidate for _, candidate in entries[:limit]]
    return output


def _core_value_queue(
    *,
    lane_toplists: Mapping[str, list[dict[str, object]]],
    ranked_candidates: Sequence[dict[str, object]],
    lane_order: Sequence[str],
) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    selected_tickers: set[str] = set()
    for lane in lane_order:
        for candidate in lane_toplists.get(lane, ()):
            if candidate.get("suppressed") is True:
                continue
            ticker = _string_value(candidate.get("ticker"))
            if ticker is None or ticker in selected_tickers:
                continue
            selected.append(_with_recommendation_lane(candidate, lane))
            selected_tickers.add(ticker)
            break
    for candidate in ranked_candidates:
        ticker = _string_value(candidate.get("ticker"))
        if ticker is None or ticker in selected_tickers:
            continue
        selected.append(_with_recommendation_lane(candidate, "global_rank_fallback"))
        selected_tickers.add(ticker)
    return selected


def _fast_dislocation_queue(candidates: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    entries = [
        (
            (
                _macro_rank(_string_value(candidate.get("outlook_sector"))),
                _fast_confidence_rank(candidate),
                -_fast_guard_count(candidate),
                _most_negative_price_change(candidate),
                _string_value(candidate.get("ticker")) or "",
            ),
            _with_recommendation_lane(candidate, "fast_dislocation_queue"),
        )
        for candidate in candidates
        if _fast_lens(candidate).get("eligible") is True
    ]
    entries.sort(key=lambda item: item[0])
    return [candidate for _, candidate in entries]


def _long_hold_survivability_queue(
    candidates: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    entries = []
    for candidate in candidates:
        lens = _long_hold_lens(candidate)
        rating = _string_value(lens.get("rating"))
        if rating not in {"high", "medium"}:
            continue
        entries.append(
            (
                (
                    0 if rating == "high" else 1,
                    _macro_rank(_string_value(candidate.get("outlook_sector"))),
                    _lane_rank(_string_value(candidate.get("selection_lane"))),
                    -_int_or(lens.get("support_count"), 0),
                    _string_value(candidate.get("ticker")) or "",
                ),
                _with_recommendation_lane(candidate, "long_hold_survivability_queue"),
            )
        )
    entries.sort(key=lambda item: item[0])
    return [candidate for _, candidate in entries]


def _deferred_revisit_queue(candidates: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    output = []
    for candidate in candidates:
        prior = candidate.get("prior_research")
        if not isinstance(prior, Mapping) or prior.get("outcome") != "deferred":
            continue
        output.append(_with_recommendation_lane(candidate, "deferred_revisit_queue"))
    return output


def _recommended_research_candidates(
    *,
    queue_map: Mapping[str, Sequence[dict[str, object]]],
    queue_order: Sequence[str],
    ranked_candidates: Sequence[dict[str, object]],
    lane_order: Sequence[str],
    diversity_rules: SelectionDiversityRules,
    min_count: int,
    limit: int,
) -> list[dict[str, object]]:
    if limit < 1:
        return []
    selected: list[dict[str, object]] = []
    selected_tickers: set[str] = set()
    sector_counts: Counter[str] = Counter()
    lane_counts: Counter[str] = Counter()
    previous_candidate_count = 0

    def can_add(candidate: Mapping[str, object], *, enforce_diversity: bool) -> bool:
        ticker = _string_value(candidate.get("ticker"))
        if ticker is None or ticker in selected_tickers:
            return False
        if not enforce_diversity:
            return True
        sector = _string_value(candidate.get("sector_33")) or ""
        lane = _string_value(candidate.get("selection_lane")) or ""
        max_sector = diversity_rules.max_recommended_per_sector
        max_lane = diversity_rules.max_recommended_per_lane
        max_previous = diversity_rules.max_previous_candidates_in_recommended
        if (
            max_previous is not None
            and candidate.get("previous_candidate") is True
            and previous_candidate_count >= max_previous
        ):
            return False
        return sector_counts[sector] < max_sector and lane_counts[lane] < max_lane

    def add(candidate: Mapping[str, object]) -> None:
        nonlocal previous_candidate_count
        ticker = _string_value(candidate.get("ticker"))
        if ticker is None:
            return
        selection_lane, selection_metrics = _primary_evidence_by_lane_order(
            candidate.get("evidence_hits"), lane_order
        )
        output = dict(candidate)
        if selection_lane is not None:
            output["selection_lane"] = selection_lane
            output["selection_metrics"] = selection_metrics
        selected.append(output)
        selected_tickers.add(ticker)
        sector_counts[_string_value(candidate.get("sector_33")) or ""] += 1
        lane_counts[_string_value(output.get("selection_lane")) or ""] += 1
        if candidate.get("previous_candidate") is True:
            previous_candidate_count += 1

    for queue_name in queue_order:
        if len(selected) >= limit:
            break
        for candidate in queue_map.get(queue_name, ()):
            if len(selected) >= limit:
                break
            if can_add(candidate, enforce_diversity=True):
                add(candidate)

    if len(selected) < min_count:
        for queue_name in queue_order:
            if len(selected) >= min(min_count, limit):
                break
            for candidate in queue_map.get(queue_name, ()):
                if len(selected) >= min(min_count, limit):
                    break
                if can_add(candidate, enforce_diversity=False):
                    add(candidate)

    for candidate in ranked_candidates:
        if len(selected) >= limit:
            break
        fallback = _with_recommendation_lane(candidate, "global_rank_fallback")
        if can_add(fallback, enforce_diversity=True):
            add(fallback)
    if len(selected) < min_count:
        for candidate in ranked_candidates:
            if len(selected) >= min(min_count, limit):
                break
            fallback = _with_recommendation_lane(candidate, "global_rank_fallback")
            if can_add(fallback, enforce_diversity=False):
                add(fallback)
    return selected


def _diagnostics(
    *,
    recommended: Sequence[dict[str, object]],
    ranked_candidates: Sequence[dict[str, object]],
    previous_candidates: PreviousCandidates,
    diversity_warning_ratio: float,
    profile: str,
) -> dict[str, object]:
    recommended_tickers = {
        ticker for item in recommended if (ticker := _string_value(item.get("ticker"))) is not None
    }
    previous_tickers = set(previous_candidates.tickers)
    overlap_tickers = sorted(recommended_tickers & previous_tickers)
    overlap_ratio = len(overlap_tickers) / len(recommended_tickers) if recommended_tickers else 0.0
    warnings = []
    if overlap_ratio >= diversity_warning_ratio and recommended_tickers:
        warnings.append("recommended_queue_high_previous_overlap")
    fallback_count = sum(
        1
        for candidate in ranked_candidates
        if _fast_lens(candidate).get("data_status") == "fallback_4w_or_60d"
    )
    if fallback_count:
        warnings.append("fast_dislocation_uses_legacy_price_change_fallback")
    return {
        "profile": profile,
        "warnings": warnings,
        "previous_overlap": {
            "previous_candidates_ref": previous_candidates.ref_path,
            "overlap_count": len(overlap_tickers),
            "overlap_ratio": round(overlap_ratio, 4),
            "overlap_tickers": overlap_tickers,
        },
        "concentration": {
            "recommended_by_sector": dict(
                Counter(_string_value(item.get("sector_33")) or "" for item in recommended)
            ),
            "recommended_by_selection_lane": dict(
                Counter(_string_value(item.get("selection_lane")) or "" for item in recommended)
            ),
        },
        "suppressed_count": sum(1 for candidate in ranked_candidates if candidate["suppressed"]),
        "legacy_price_fallback_candidate_count": fallback_count,
    }


def _long_hold_counts(candidates: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for candidate in candidates:
        rating = _string_value(_long_hold_lens(candidate).get("rating")) or "unknown"
        counts[rating] += 1
    return dict(counts)


def _with_recommendation_lane(
    candidate: Mapping[str, object],
    recommendation_lane: str,
) -> dict[str, object]:
    output = dict(candidate)
    output["recommendation_lane"] = recommendation_lane
    return output


def _fast_lens(candidate: Mapping[str, object]) -> Mapping[str, object]:
    lenses = candidate.get("lenses")
    if not isinstance(lenses, Mapping):
        return {}
    lens = lenses.get("fast_dislocation")
    return lens if isinstance(lens, Mapping) else {}


def _long_hold_lens(candidate: Mapping[str, object]) -> Mapping[str, object]:
    lenses = candidate.get("lenses")
    if not isinstance(lenses, Mapping):
        return {}
    lens = lenses.get("long_hold_survivability")
    return lens if isinstance(lens, Mapping) else {}


def _fast_confidence_rank(candidate: Mapping[str, object]) -> int:
    match _string_value(_fast_lens(candidate).get("confidence")):
        case "high":
            return 0
        case "medium":
            return 1
        case _:
            return 2


def _fast_guard_count(candidate: Mapping[str, object]) -> int:
    count = _fast_lens(candidate).get("fundamental_guard_count")
    return int(count) if isinstance(count, int) else 0


def _most_negative_price_change(candidate: Mapping[str, object]) -> float:
    values = [
        _number(candidate.get("price_change_1d")),
        _number(candidate.get("price_change_5d")),
        _number(candidate.get("price_change_20d")),
        _number(candidate.get("price_change_60d")),
    ]
    available = [value for value in values if value is not None]
    return min(available) if available else 0.0


def _research_recommendation_limit(*, top: int, configured_max: int) -> int:
    return min(top, configured_max) if configured_max > 0 else top


def _primary_evidence_by_lane_order(
    raw_evidence_hits: object,
    lane_order: Sequence[str],
) -> tuple[str | None, dict[str, object]]:
    if not isinstance(raw_evidence_hits, Sequence) or isinstance(raw_evidence_hits, str):
        return None, {}
    evidence_by_lane: dict[str, Mapping[str, object]] = {}
    for evidence_hit in raw_evidence_hits:
        if not isinstance(evidence_hit, Mapping):
            continue
        if not _is_sizing_eligible_evidence(evidence_hit):
            continue
        name = _string_value(evidence_hit.get("name"))
        if name is None:
            continue
        evidence_by_lane[name] = evidence_hit
    for lane in lane_order:
        evidence_hit = evidence_by_lane.get(lane)
        if evidence_hit is not None:
            return lane, _metric_map(evidence_hit.get("metrics"))
    return None, {}


def _best_selection_evidence(
    evidence_hits: Sequence[Mapping[str, object]],
) -> tuple[str | None, dict[str, object], tuple[float, ...]]:
    entries = [
        (
            _lane_rank(name),
            _evidence_strength_key(name, metrics),
            name,
            metrics,
        )
        for evidence_hit in evidence_hits
        if (name := _string_value(evidence_hit.get("name"))) is not None
        for metrics in [_metric_map(evidence_hit.get("metrics"))]
    ]
    if not entries:
        return None, {}, (0.0,)
    _, strength_key, name, metrics = min(entries, key=lambda item: (item[0], item[1]))
    return name, metrics, strength_key


def _sizing_eligible_evidence_hits(
    evidence_hits: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    return tuple(hit for hit in evidence_hits if _is_sizing_eligible_evidence(hit))


def _is_sizing_eligible_evidence(evidence_hit: Mapping[str, object]) -> bool:
    source_status = evidence_hit.get("source_status")
    if isinstance(source_status, str) and source_status != "ok":
        return False
    return evidence_hit.get("sizing_eligible") is not False


def _macro_rank(status: str | None) -> int:
    match status:
        case "supportive":
            return 0
        case "neutral":
            return 1
        case _:
            return 2


def _lane_rank(name: str | None) -> int:
    return _LANE_RANK.get(name or "", 99)


def _evidence_strength_key(name: str, metrics: Mapping[str, object]) -> tuple[float, ...]:
    match name:
        case "valuation-reversion":
            return (
                _float_or(metrics.get("condition_a_sector_median_gap"), 1.0),
                _float_or(metrics.get("condition_a_self_range_percentile"), 1.0),
                _float_or(metrics.get("condition_b_sigma_gap"), 1.0),
                _float_or(metrics.get("price_change_60d"), 1.0),
            )
        case "cash-rich-asset-discount":
            return (
                -_float_or(metrics.get("cash_to_market_cap"), 0.0),
                _float_or(metrics.get("price_to_equity"), 99.0),
            )
        case "strict-net-cash-discount":
            return (
                -_float_or(metrics.get("net_cash_to_market_cap"), 0.0),
                _float_or(metrics.get("price_to_equity"), 99.0),
            )
        case "cashflow-yield-discount":
            return (
                -_float_or(metrics.get("ocf_yield"), 0.0),
                -_float_or(metrics.get("cfo_yoy"), -99.0),
            )
        case "fcf-yield-discount":
            return (
                -_float_or(metrics.get("fcf_yield"), 0.0),
                -_float_or(metrics.get("cfo_yoy"), -99.0),
            )
        case "sales-discount-growth":
            operating_profit = _float_or(metrics.get("operating_profit"), -1.0)
            return (
                _float_or(metrics.get("ps_sector_gap"), 1.0),
                -_float_or(metrics.get("sales_yoy"), 0.0),
                0.0 if operating_profit >= 0 else 1.0,
            )
        case _:
            return (0.0,)


def _dict_sequence(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _metric_map(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _float_or(value: object, default: float) -> float:
    number = _number(value)
    return number if number is not None else default


def _int_or(value: object, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _date_from_datetime_prefix(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return _parse_date(value)


def _deep_merge(
    base: Mapping[str, object],
    override: Mapping[str, object],
) -> dict[str, object]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged
