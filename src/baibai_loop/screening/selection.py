from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

from baibai_loop.macro_context import MacroContext, macro_context_diagnostics

from .rule_config import (
    BUILTIN_SELECTION_PROFILES,
    ScreeningRules,
    SelectionDiversityRules,
    SelectionRules,
)
from .tiers import position_tier

_LANE_RANK = {
    "valuation-reversion": 0,
    "strict-net-cash-discount": 1,
    "fcf-yield-discount": 2,
    "cash-rich-asset-discount": 3,
    "cashflow-yield-discount": 4,
    "sales-discount-growth": 5,
}

BALANCED_PROFILE_OVERRIDES: Mapping[str, object] = {
    "fast_dislocation": {
        "price_change_1d_max": -0.05,
        "price_change_5d_max": -0.08,
        "price_change_20d_max": -0.10,
        "price_change_60d_max": -0.22,
        "gap_from_52w_low_max": 0.15,
        "turnover_spike_5d_min": 1.5,
        "min_fundamental_guard_count": 2,
        "min_fundamental_guard_family_count": 2,
        "high_confidence_guard_count": 3,
        "high_confidence_guard_family_count": 2,
        "ocf_yield_min": 0.08,
        "fcf_yield_min": 0.05,
        "price_to_equity_max": 1.0,
        "equity_ratio_min": 0.4,
        "net_cash_to_market_cap_min": 0.2,
        "sales_yoy_min": 0.05,
        "operating_profit_positive_required": True,
    },
    "diversity": {
        "max_recommended_per_sector": 1,
        "max_recommended_per_lane": 2,
        "max_previous_candidates_in_recommended": 2,
        "previous_overlap_warning_ratio": 0.6,
    },
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
            "min_fundamental_guard_family_count": 2,
            "high_confidence_guard_family_count": 2,
            "ocf_yield_min": 0.10,
            "fcf_yield_min": 0.07,
            "equity_ratio_min": 0.45,
            "price_to_equity_max": 1.0,
            "net_cash_to_market_cap_min": 0.25,
            "sales_yoy_min": 0.05,
        },
        "diversity": {
            "max_recommended_per_sector": 1,
            "max_recommended_per_lane": 1,
            "max_previous_candidates_in_recommended": 2,
        },
    },
    "balanced": BALANCED_PROFILE_OVERRIDES,
    "loose": {
        "fast_dislocation": {
            "price_change_1d_max": -0.03,
            "price_change_5d_max": -0.05,
            "price_change_20d_max": -0.08,
            "price_change_60d_max": -0.12,
            "gap_from_52w_low_max": 0.25,
            "turnover_spike_5d_min": None,
            "min_fundamental_guard_count": 1,
            "min_fundamental_guard_family_count": 1,
            "high_confidence_guard_count": 2,
            "ocf_yield_min": 0.05,
            "fcf_yield_min": 0.03,
            "equity_ratio_min": 0.35,
            "price_to_equity_max": 1.1,
            "net_cash_to_market_cap_min": 0.10,
            "sales_yoy_min": 0.03,
        },
        "diversity": {
            "max_recommended_per_sector": 3,
            "max_recommended_per_lane": 3,
            "max_previous_candidates_in_recommended": None,
        },
    },
}

if set(BUILTIN_PROFILE_OVERRIDES) != BUILTIN_SELECTION_PROFILES:
    raise RuntimeError("BUILTIN_PROFILE_OVERRIDES must match BUILTIN_SELECTION_PROFILES")


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
    data["default_profile"] = (
        profile if profile in BUILTIN_SELECTION_PROFILES else base.default_profile
    )
    return SelectionRules.model_validate(data)


def build_selection_payload(
    *,
    asof_date: date,
    candidates: Sequence[CandidateRecord],
    macro_context: MacroContext | None,
    rules: ScreeningRules,
    top: int,
    profile: str | None,
    candidates_ref: str,
    macro_context_ref: str | None,
    previous_candidates: PreviousCandidates | None = None,
    prior_research_by_ticker: Mapping[str, PriorResearch] | None = None,
    profile_overrides: Mapping[str, Mapping[str, object]] | None = None,
    detail: str = "summary",
) -> dict[str, object]:
    if detail not in {"summary", "full"}:
        raise ValueError("detail must be summary or full")
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
    macro_context_checked_count = 0

    for item in candidates:
        macro_context_checked_count += 1
        macro_context_result = _candidate_macro_context_result(
            item,
            macro_context=macro_context,
            asof_date=asof_date,
        )
        macro_context_alignment = _macro_context_alignment(macro_context_result)
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
            macro_context_result=macro_context_result,
            macro_context_alignment=macro_context_alignment,
            selection_lane=selection_lane,
            selection_metrics=selection_metrics,
            lenses=lenses,
            prior_research=prior_research,
            suppressed=suppress_reason is not None,
            suppression_reasons=(suppress_reason,) if suppress_reason else (),
            previous_candidate=item.ticker in previous_tickers,
        )
        sort_key = (
            _macro_rank(macro_context_alignment),
            0 if _fast_lens(candidate).get("eligible") is True else 1,
            _long_hold_rank(candidate),
            _lane_rank(selection_lane),
            *strength_key,
            -len(eligible_evidence_hits),
            item.ticker,
        )
        ranked_entries.append((sort_key, candidate))

    ranked_entries.sort(key=lambda item: item[0])
    ranked_candidates = [candidate for _, candidate in ranked_entries]
    ranked_active_candidates = [
        candidate for candidate in ranked_candidates if not candidate["suppressed"]
    ]
    recommended = _recommended_research_candidates(
        ranked_candidates=ranked_active_candidates,
        lane_order=rules.output.research_selection_lane_order,
        diversity_rules=selection_rules.diversity,
        limit=recommendation_limit,
    )
    diagnostics = _diagnostics(
        recommended=recommended,
        ranked_candidates=ranked_candidates,
        previous_candidates=previous_candidates,
        diversity_warning_ratio=selection_rules.diversity.previous_overlap_warning_ratio,
        profile=effective_profile,
    )
    recommendations = (
        recommended
        if detail == "full"
        else [
            _selection_candidate_summary(candidate, rank=rank)
            for rank, candidate in enumerate(recommended, start=1)
        ]
    )
    return {
        "recommendations": recommendations,
        "selection": {
            "asof": asof_date.isoformat(),
            "profile": effective_profile,
            "input_refs": {
                "candidates_ref": candidates_ref,
                "macro_context_ref": macro_context_ref,
                "previous_candidates_ref": previous_candidates.ref_path,
            },
            "counts": {
                "input": len(candidates),
                "after_macro_context_check": macro_context_checked_count,
                "after_evidence_filter": len(ranked_candidates),
            },
            "research_selection_target_max": rules.output.research_selection_target_max,
            "research_selection_lane_order": list(rules.output.research_selection_lane_order),
            "macro_context_summary": _macro_context_summary(macro_context),
            "diagnostics": diagnostics,
            "detail": detail,
        },
    }


def build_selection_sweep_payload(
    *,
    asof_date: date,
    candidates: Sequence[CandidateRecord],
    macro_context: MacroContext | None,
    rules: ScreeningRules,
    top: int,
    profiles: Sequence[str],
    candidates_ref: str,
    macro_context_ref: str | None,
    previous_candidates: PreviousCandidates | None = None,
    prior_research_by_ticker: Mapping[str, PriorResearch] | None = None,
    profile_overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    profile_results: list[dict[str, object]] = []
    for profile in profiles:
        payload = build_selection_payload(
            asof_date=asof_date,
            candidates=candidates,
            macro_context=macro_context,
            rules=rules,
            top=top,
            profile=profile,
            candidates_ref=candidates_ref,
            macro_context_ref=macro_context_ref,
            previous_candidates=previous_candidates,
            prior_research_by_ticker=prior_research_by_ticker,
            profile_overrides=profile_overrides,
            detail="full",
        )
        selection = _mapping(payload.get("selection"))
        diagnostics = _mapping(selection.get("diagnostics"))
        recommended = _dict_sequence(payload.get("recommendations"))
        profile_results.append(
            {
                "profile": profile,
                "recommended": [
                    _sweep_candidate_summary(item, rank=index)
                    for index, item in enumerate(recommended, start=1)
                ],
                "recommended_tickers": [_string_value(item.get("ticker")) for item in recommended],
                "recommended_count": len(recommended),
                "fast_dislocation_count": _int_or(diagnostics.get("fast_dislocation_count"), 0),
                "long_hold_counts": dict(_mapping(diagnostics.get("long_hold_counts"))),
                "suppressed_count": _int_or(diagnostics.get("suppressed_count"), 0),
                "previous_overlap": diagnostics.get("previous_overlap"),
                "concentration": diagnostics.get("concentration"),
                "warnings": diagnostics.get("warnings"),
            }
        )
    if profile_results:
        base_tickers = set(_string_sequence(profile_results[0].get("recommended_tickers")))
        base_by_ticker = {
            ticker: item
            for item in _dict_sequence(profile_results[0].get("recommended"))
            if (ticker := _string_value(item.get("ticker"))) is not None
        }
        for result in profile_results:
            tickers = set(_string_sequence(result.get("recommended_tickers")))
            current_by_ticker = {
                ticker: item
                for item in _dict_sequence(result.get("recommended"))
                if (ticker := _string_value(item.get("ticker"))) is not None
            }
            result["recommended_diff_vs_first_profile"] = {
                "added": sorted(tickers - base_tickers),
                "removed": sorted(base_tickers - tickers),
                "changed": _sweep_changed_summaries(base_by_ticker, current_by_ticker),
            }
    return {
        "asof": asof_date.isoformat(),
        "input_refs": {
            "candidates_ref": candidates_ref,
            "macro_context_ref": macro_context_ref,
            "previous_candidates_ref": previous_candidates.ref_path
            if previous_candidates is not None
            else None,
        },
        "macro_context_summary": _macro_context_summary(macro_context),
        "profiles": profile_results,
    }


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


def _candidate_lenses(item: CandidateRecord, rules: SelectionRules) -> dict[str, object]:
    return {
        "fast_dislocation": _fast_dislocation_lens(item, rules),
        "long_hold_survivability": _long_hold_survivability_lens(item, rules),
    }


def _fast_dislocation_lens(item: CandidateRecord, rules: SelectionRules) -> dict[str, object]:
    lane_rules = rules.fast_dislocation
    price_triggers: list[dict[str, object]] = []
    auxiliary_triggers: list[dict[str, object]] = []
    trigger_specs = (
        ("price_change_1d", item.price_change_1d, lane_rules.price_change_1d_max),
        ("price_change_5d", item.price_change_5d, lane_rules.price_change_5d_max),
        ("price_change_20d", item.price_change_20d, lane_rules.price_change_20d_max),
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
        auxiliary_triggers.append(
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
        auxiliary_triggers.append(
            {
                "metric": "turnover_spike_5d",
                "value": item.turnover_spike_5d,
                "threshold": lane_rules.turnover_spike_5d_min,
            }
        )

    guard_reasons = _fundamental_guard_reasons(item, rules)
    guard_count = len(guard_reasons)
    guard_families = sorted({_fundamental_guard_family(reason) for reason in guard_reasons})
    stale_fundamental_metrics = _has_edinet_freshness_warning(item)
    eligible = (
        lane_rules.enabled
        and bool(price_triggers)
        and guard_count >= lane_rules.min_fundamental_guard_count
        and len(guard_families) >= lane_rules.min_fundamental_guard_family_count
    )
    confidence = "none"
    if eligible:
        high_confidence = (
            guard_count >= lane_rules.high_confidence_guard_count
            and len(guard_families) >= lane_rules.high_confidence_guard_family_count
            and not stale_fundamental_metrics
        )
        confidence = "high" if high_confidence else "medium"
    data_status = _fast_dislocation_data_status(item)
    if stale_fundamental_metrics:
        data_status = "stale_fundamental_metrics"
    return {
        "eligible": eligible,
        "confidence": confidence,
        "price_triggers": price_triggers,
        "auxiliary_triggers": auxiliary_triggers,
        "fundamental_guard_count": guard_count,
        "fundamental_guard_family_count": len(guard_families),
        "fundamental_guard_families": guard_families,
        "fundamental_guard_reasons": guard_reasons,
        "stale_fundamental_metrics": stale_fundamental_metrics,
        "data_status": data_status,
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
    sales_yoy = _number(metrics.get("sales_yoy"))
    if sales_yoy is not None and sales_yoy >= lane_rules.sales_yoy_min and operating_profit_ok:
        reasons.append("sales_growth_with_profit")
    return reasons


def _fundamental_guard_family(reason: str) -> str:
    if reason in {"fcf_yield", "ocf_yield"}:
        return "cash_flow"
    if reason in {"asset_discount_with_equity_buffer", "net_cash_buffer"}:
        return "balance_sheet"
    if reason == "sales_growth_with_profit":
        return "profitability"
    return "other"


def _has_edinet_freshness_warning(item: CandidateRecord) -> bool:
    return any(
        _string_value(warning.get("stale_metric")) == "edinet_metrics"
        for warning in item.freshness_warnings
    )


def _fast_dislocation_data_status(item: CandidateRecord) -> str:
    if item.price_change_5d is None and item.price_change_20d is None:
        return (
            "short_return_pipeline_missing"
            if item.price_change_60d is not None
            else "missing_price_history"
        )
    if item.price_change_20d is None:
        return "short_return_pipeline_missing"
    return "ok"


def _long_hold_survivability_lens(
    item: CandidateRecord,
    rules: SelectionRules,
) -> dict[str, object]:
    lens_rules = rules.long_hold_survivability
    metrics = item.metrics
    reasons: list[str] = []
    missing_reasons: list[str] = []
    weak_reasons: list[str] = []
    equity_ratio = _number(metrics.get("equity_ratio"))
    if equity_ratio is None:
        missing_reasons.append("equity_ratio_missing")
    elif equity_ratio >= lens_rules.equity_ratio_high_min:
        reasons.append("high_equity_ratio")
    elif equity_ratio >= lens_rules.equity_ratio_medium_min:
        reasons.append("medium_equity_ratio")
    else:
        weak_reasons.append("low_equity_ratio")

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
    ocf_yield = _number(metrics.get("ocf_yield"))
    if ocf_yield is None:
        missing_reasons.append("ocf_yield_missing")
    elif ocf_yield > lens_rules.ocf_yield_positive_min:
        reasons.append("positive_ocf_yield")
    else:
        weak_reasons.append("weak_ocf_yield")
    if _float_or(metrics.get("fcf_yield"), -1.0) > lens_rules.fcf_yield_positive_min:
        reasons.append("positive_fcf_yield")
    operating_profit = _number(metrics.get("operating_profit"))
    if operating_profit is None:
        missing_reasons.append("operating_profit_missing")
    elif operating_profit > 0:
        reasons.append("positive_operating_profit")
    else:
        weak_reasons.append("operating_profit_not_positive")
    if item.avg_turnover_oku is None:
        missing_reasons.append("liquidity_missing")
    elif item.avg_turnover_oku >= lens_rules.min_avg_turnover_oku:
        reasons.append("liquidity_pass")
    else:
        weak_reasons.append("liquidity_low")

    support_count = len(set(reasons))
    caution_reasons = sorted({*missing_reasons, *weak_reasons})
    if support_count >= lens_rules.high_min_support_count and len(caution_reasons) <= 1:
        rating = "high"
    elif support_count >= lens_rules.medium_min_support_count:
        rating = "medium"
    elif support_count == 0 and len(missing_reasons) >= 3:
        rating = "unknown"
    else:
        rating = "low"
    return {
        "rating": rating,
        "support_count": support_count,
        "reasons": sorted(set(reasons)),
        "missing_reasons": sorted(set(missing_reasons)),
        "weak_reasons": sorted(set(weak_reasons)),
        "caution_reasons": caution_reasons,
    }


def _selection_candidate(
    item: CandidateRecord,
    *,
    macro_context_result: Mapping[str, object],
    macro_context_alignment: str,
    selection_lane: str | None,
    selection_metrics: Mapping[str, object],
    lenses: Mapping[str, object],
    prior_research: PriorResearch | None,
    suppressed: bool,
    suppression_reasons: Sequence[str],
    previous_candidate: bool,
) -> dict[str, object]:
    output: dict[str, object] = {
        "ticker": item.ticker,
        "name": item.name,
        "sector_33": item.sector_33,
        "macro_context_alignment": macro_context_alignment,
        "macro_context": dict(macro_context_result),
        "market_cap_oku": item.market_cap_oku,
        "price_change_1d": item.price_change_1d,
        "price_change_5d": item.price_change_5d,
        "price_change_20d": item.price_change_20d,
        "price_change_60d": item.price_change_60d,
        "gap_from_52w_low": item.gap_from_52w_low,
        "turnover_spike_5d": item.turnover_spike_5d,
        "evidence_hits": list(item.evidence_hits),
        "freshness_warnings": list(item.freshness_warnings),
        "selection_lane": selection_lane,
        "selection_metrics": dict(selection_metrics),
        "next_earnings_date": item.next_earnings_date,
        "position_tier": position_tier(item.market_cap_oku),
        "lenses": dict(lenses),
        "prior_research": prior_research.to_dict() if prior_research is not None else None,
        "previous_candidate": previous_candidate,
        "suppressed": suppressed,
        "suppression_reasons": list(suppression_reasons),
    }
    metric_type_warnings = _numeric_metric_type_warnings(item.metrics, source="metrics")
    metric_type_warnings.extend(_evidence_metric_type_warnings(item.evidence_hits))
    if metric_type_warnings:
        output["metric_type_warnings"] = metric_type_warnings
    output["reason_tags"] = _candidate_reason_tags(output)
    output["risk_tags"] = _candidate_risk_tags(output)
    return output


def _recommended_research_candidates(
    *,
    ranked_candidates: Sequence[dict[str, object]],
    lane_order: Sequence[str],
    diversity_rules: SelectionDiversityRules,
    limit: int,
) -> list[dict[str, object]]:
    if limit < 1:
        return []
    selected: list[dict[str, object]] = []
    selected_tickers: set[str] = set()
    sector_counts: Counter[str] = Counter()
    lane_counts: Counter[str] = Counter()
    previous_candidate_count = 0

    def normalized_candidate(candidate: Mapping[str, object]) -> dict[str, object]:
        selection_lane, selection_metrics = _primary_evidence_by_lane_order(
            candidate.get("evidence_hits"), lane_order
        )
        output = dict(candidate)
        if selection_lane is not None:
            output["selection_lane"] = selection_lane
            output["selection_metrics"] = selection_metrics
        return output

    def can_add(candidate: Mapping[str, object], *, enforce_diversity: bool) -> bool:
        ticker = _string_value(candidate.get("ticker"))
        if ticker is None or ticker in selected_tickers:
            return False
        output = normalized_candidate(candidate)
        if not enforce_diversity:
            return True
        sector = _string_value(candidate.get("sector_33")) or ""
        lane = _string_value(output.get("selection_lane")) or ""
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
        output = normalized_candidate(candidate)
        selected.append(output)
        selected_tickers.add(ticker)
        sector_counts[_string_value(candidate.get("sector_33")) or ""] += 1
        lane_counts[_string_value(output.get("selection_lane")) or ""] += 1
        if candidate.get("previous_candidate") is True:
            previous_candidate_count += 1

    for candidate in ranked_candidates:
        if len(selected) >= limit:
            break
        if can_add(candidate, enforce_diversity=True):
            add(candidate)
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
        warnings.append("recommendations_high_previous_overlap")
    metric_type_warning_count = sum(
        len(_dict_sequence(candidate.get("metric_type_warnings")))
        for candidate in ranked_candidates
    )
    if metric_type_warning_count:
        warnings.append("invalid_numeric_metric_values")
    fast_data_status_counts = Counter(
        _string_value(_fast_lens(candidate).get("data_status")) or "unknown"
        for candidate in ranked_candidates
    )
    short_return_missing_count = sum(
        fast_data_status_counts[status]
        for status in ("missing_price_history", "short_return_pipeline_missing")
    )
    if short_return_missing_count:
        warnings.append("short_return_price_history_missing")
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
        "fast_dislocation_count": sum(
            1 for candidate in ranked_candidates if _fast_lens(candidate).get("eligible") is True
        ),
        "long_hold_counts": _long_hold_counts(ranked_candidates),
        "short_return_missing_candidate_count": short_return_missing_count,
        "fast_dislocation_data_status_counts": dict(fast_data_status_counts),
        "invalid_numeric_metric_value_count": metric_type_warning_count,
    }


def _long_hold_counts(candidates: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for candidate in candidates:
        rating = _string_value(_long_hold_lens(candidate).get("rating")) or "unknown"
        counts[rating] += 1
    return dict(counts)


def _candidate_reason_tags(candidate: Mapping[str, object]) -> list[str]:
    tags: list[str] = []
    if _fast_lens(candidate).get("eligible") is True:
        tags.append("fast_dislocation")
    lane = _string_value(candidate.get("selection_lane"))
    if lane:
        tags.append(lane)
    long_hold = _long_hold_lens(candidate)
    rating = _string_value(long_hold.get("rating"))
    if rating == "high":
        tags.append("long_hold_high")
    elif rating == "medium":
        tags.append("long_hold_medium")
    fast = _fast_lens(candidate)
    confidence = _string_value(fast.get("confidence"))
    if confidence == "high":
        tags.append("fast_confidence_high")
    return _dedupe_strings(tags)


def _candidate_risk_tags(candidate: Mapping[str, object]) -> list[str]:
    tags: list[str] = []
    if candidate.get("previous_candidate") is True:
        tags.append("previous_candidate")
    if candidate.get("suppressed") is True:
        tags.append("suppressed_by_prior_research")
    if _string_value(candidate.get("next_earnings_date")):
        tags.append("earnings_scheduled")
    if candidate.get("freshness_warnings"):
        tags.append("freshness_warning")
    fast = _fast_lens(candidate)
    if fast.get("stale_fundamental_metrics") is True:
        tags.append("stale_fundamental_metrics")
    return _dedupe_strings(tags)


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
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


def _long_hold_rank(candidate: Mapping[str, object]) -> int:
    match _string_value(_long_hold_lens(candidate).get("rating")):
        case "high":
            return 0
        case "medium":
            return 1
        case _:
            return 2


def _fast_guard_count(candidate: Mapping[str, object]) -> int:
    count = _fast_lens(candidate).get("fundamental_guard_count")
    return int(count) if isinstance(count, int) else 0


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


def _candidate_macro_context_result(
    item: CandidateRecord,
    *,
    macro_context: MacroContext | None,
    asof_date: date,
) -> dict[str, object]:
    if macro_context is None:
        return {
            "context_id": None,
            "stale": False,
            "matched_items": [],
            "unknown_items": [],
            "warnings": ["macro_context_missing"],
        }
    return macro_context_diagnostics(
        macro_context,
        asof_date=asof_date,
        candidate_sector=item.sector_33,
    )


def _macro_context_summary(macro_context: MacroContext | None) -> dict[str, object] | None:
    if macro_context is None:
        return None
    payload = macro_context.payload
    return {
        "context_id": macro_context.context_id,
        "as_of": macro_context.as_of.isoformat(),
        "valid_until": macro_context.valid_until.isoformat(),
        "research_questions": _string_sequence(payload.get("research_questions")),
        "refresh_triggers": _string_sequence(payload.get("refresh_triggers")),
    }


def _macro_context_alignment(result: Mapping[str, object]) -> str:
    items = _dict_sequence(result.get("matched_items"))
    stances = {_string_value(item.get("stance")) for item in items}
    if stances & {"tailwind"}:
        return "tailwind"
    if stances & {"headwind"}:
        return "headwind"
    if stances & {"mixed"}:
        return "mixed"
    if stances & {"neutral"}:
        return "neutral"
    return "not_matched"


def _macro_rank(status: str | None) -> int:
    match status:
        case "tailwind":
            return 0
        case "neutral" | "mixed" | "not_matched":
            return 1
        case "headwind":
            return 2
        case _:
            return 3


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


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    return tuple(item for item in value if isinstance(item, str))


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


def _selection_candidate_summary(
    candidate: Mapping[str, object], *, rank: int
) -> dict[str, object]:
    fast_lens = _fast_lens(candidate)
    long_hold_lens = _long_hold_lens(candidate)
    return {
        "rank": rank,
        "ticker": _string_value(candidate.get("ticker")),
        "name": _string_value(candidate.get("name")),
        "sector_33": _string_value(candidate.get("sector_33")),
        "selection_lane": _string_value(candidate.get("selection_lane")),
        "macro_context_alignment": _string_value(candidate.get("macro_context_alignment")),
        "market_cap_oku": candidate.get("market_cap_oku"),
        "price_change_5d": candidate.get("price_change_5d"),
        "price_change_20d": candidate.get("price_change_20d"),
        "gap_from_52w_low": candidate.get("gap_from_52w_low"),
        "next_earnings_date": candidate.get("next_earnings_date"),
        "position_tier": candidate.get("position_tier"),
        "fast_confidence": _string_value(fast_lens.get("confidence")),
        "fast_guard_count": _fast_guard_count(candidate),
        "fast_guard_family_count": _int_or(fast_lens.get("fundamental_guard_family_count"), 0),
        "fast_data_status": _string_value(fast_lens.get("data_status")),
        "long_hold_rating": _string_value(long_hold_lens.get("rating")),
        "prior_research": candidate.get("prior_research"),
        "previous_candidate": candidate.get("previous_candidate") is True,
        "suppressed": candidate.get("suppressed") is True,
        "suppression_reasons": list(_string_sequence(candidate.get("suppression_reasons"))),
        "reason_tags": list(_string_sequence(candidate.get("reason_tags"))),
        "risk_tags": list(_string_sequence(candidate.get("risk_tags"))),
    }


def _sweep_candidate_summary(candidate: Mapping[str, object], *, rank: int) -> dict[str, object]:
    fast_lens = _fast_lens(candidate)
    long_hold_lens = _long_hold_lens(candidate)
    return {
        "rank": rank,
        "ticker": _string_value(candidate.get("ticker")),
        "name": _string_value(candidate.get("name")),
        "selection_lane": _string_value(candidate.get("selection_lane")),
        "fast_confidence": _string_value(fast_lens.get("confidence")),
        "fast_guard_count": _fast_guard_count(candidate),
        "fast_guard_family_count": _int_or(fast_lens.get("fundamental_guard_family_count"), 0),
        "fast_data_status": _string_value(fast_lens.get("data_status")),
        "stale_fundamental_metrics": fast_lens.get("stale_fundamental_metrics") is True,
        "long_hold_rating": _string_value(long_hold_lens.get("rating")),
        "previous_candidate": candidate.get("previous_candidate") is True,
        "reason_tags": list(_string_sequence(candidate.get("reason_tags"))),
        "risk_tags": list(_string_sequence(candidate.get("risk_tags"))),
    }


def _sweep_changed_summaries(
    base_by_ticker: Mapping[str, Mapping[str, object]],
    current_by_ticker: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    changed: list[dict[str, object]] = []
    for ticker in sorted(set(base_by_ticker) & set(current_by_ticker)):
        base = base_by_ticker[ticker]
        current = current_by_ticker[ticker]
        changed_fields = {
            key
            for key in (
                "fast_confidence",
                "fast_guard_count",
                "fast_guard_family_count",
                "fast_data_status",
                "long_hold_rating",
                "selection_lane",
                "rank",
                "stale_fundamental_metrics",
            )
            if base.get(key) != current.get(key)
        }
        if not changed_fields:
            continue
        changed.append(
            {
                "ticker": ticker,
                "changed_fields": sorted(changed_fields),
                "from": {key: base.get(key) for key in sorted(changed_fields)},
                "to": {key: current.get(key) for key in sorted(changed_fields)},
            }
        )
    return changed


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
