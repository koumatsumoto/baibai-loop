"""Produce the exact nomination union consumed by Research Triage."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from hashlib import sha256
from math import isfinite
from statistics import median
from typing import NamedTuple

from baibai_engine.foundation.coerce import metric_map, optional_float, string_or_none
from baibai_engine.screening.discovery.contracts import (
    APPROACH_IDS,
    Nomination,
    PublishedReviewSet,
    ReinvestmentAnalysis,
    ReviewSetAnalysis,
    ReviewSetEntry,
)
from baibai_engine.screening.rule_config import CandidateDiscoveryRules
from baibai_engine.screening.universe import UNIVERSE_SCOPE_REVISION
from baibai_engine.storage.application.json import canonical_json
from baibai_engine.valuation.metrics.relative import (
    MIN_SECTOR_MEDIAN_POPULATION,
    normalized_earnings_sector_gaps,
)
from baibai_engine.valuation.query import machine_valuation_from_analysis

# 金融の資産・調達を非金融の残余資産proxyへ投入しない (#1153)。
# docs/reference/screening-runtime.md#approach-applicability
_ASSET_VALUE_EXCLUDED_SECTORS = frozenset(
    {
        "銀行業",
        "保険業",
        "その他金融業",
        "証券・商品先物取引業",
    }
)


class _ReinvestmentValues(NamedTuple):
    p_s_gap: float
    capital_return: float
    sales_yoy: float
    operating_margin: float
    fcf_yield: float


class _QualityFloors(NamedTuple):
    capital_return: float
    operating_margin: float


class ReviewSetContractError(ValueError):
    """Raised when a Review Set cannot be reproduced from its source analyses."""


def candidate_discovery_method_hash(
    rules: CandidateDiscoveryRules,
    *,
    required_jpx_flags: Sequence[str],
) -> str:
    representation = {
        "method_id": rules.method_id,
        "universe_scope_revision": UNIVERSE_SCOPE_REVISION,
        "nomination_depth": rules.nomination_depth,
        "normalized_sector_median_min_population": MIN_SECTOR_MEDIAN_POPULATION,
        "common_eligibility": rules.common_eligibility.model_dump(mode="json"),
        "required_jpx_flags": sorted(set(required_jpx_flags)),
        "approaches": {
            key: value.model_dump(mode="json") for key, value in rules.approaches.items()
        },
        "approach_eligibility": {
            "asset-value": {
                "asset_backed_ratio": "positive_required",
                "excluded_sectors": sorted(_ASSET_VALUE_EXCLUDED_SECTORS),
                "net_cash_to_market_cap": "analysis_only",
            },
            "reinvestment-value": {
                "positive_inputs": [
                    "p_s",
                    "sales_yoy",
                    "operating_profit_ttm",
                    "sales_ttm",
                    "same_state_equity_yen",
                ],
                "finite_inputs": ["p_s_sector_gap", "fcf_yield", "debt", "cash"],
                "p_s_sector_gap": "negative_required",
                "operating_margin": "sector_median_or_market_fallback_inclusive",
                "operating_return_on_capital_proxy": ("sector_median_or_market_fallback_inclusive"),
                "median_population": "common_eligible_with_resolved_reinvestment_inputs",
                "sector_median_min_population": MIN_SECTOR_MEDIAN_POPULATION,
            },
        },
        "ordering": {
            "current-earnings-power": [
                "chosen_per_sector_gap_asc",
                "chosen_per_asc",
                "fcf_yield_desc_null_last",
                "ocf_yield_desc_null_last",
                "ev_ebitda_asc_null_last",
                "ticker_asc",
            ],
            "normalized-earnings-power": [
                "normalized_per_3fy_sector_gap_asc",
                "normalized_per_3fy_asc",
                "fcf_yield_desc_null_last",
                "per_trailing_asc_null_last",
                "ticker_asc",
            ],
            "asset-value": [
                "asset_backed_ratio_desc",
                "pbr_sector_gap_asc_null_last",
                "pbr_asc_null_last",
                "equity_ratio_desc_null_last",
                "ticker_asc",
            ],
            "reinvestment-value": [
                "p_s_sector_gap_asc",
                "operating_return_on_capital_proxy_desc",
                "sales_yoy_desc",
                "operating_margin_desc",
                "fcf_yield_desc",
                "ticker_asc",
            ],
        },
        "review_set_membership": "exact_nomination_union",
        "review_set_ordering": "ticker_asc_non_economic",
    }
    return sha256(canonical_json(representation).encode()).hexdigest()


def build_review_set(
    security_analyses: Sequence[Mapping[str, object]],
    *,
    rules: CandidateDiscoveryRules,
    required_jpx_flags: Sequence[str],
) -> dict[str, object]:
    eligible = _eligible_population(security_analyses, rules, required_jpx_flags)
    normalized_gaps = normalized_earnings_sector_gaps(_normalized_earnings_inputs(eligible))
    nominations_by_ticker = {
        ticker: list(nominations)
        for ticker, nominations in _nomination_ranks(
            eligible, rules=rules, normalized_gaps=normalized_gaps
        ).items()
    }
    nomination_counts = {
        approach: sum(
            any(item.valuation_approach_id == approach for item in nominations)
            for nominations in nominations_by_ticker.values()
        )
        for approach in APPROACH_IDS
    }
    by_ticker = {_ticker(row): row for row in eligible}
    pool = set(nominations_by_ticker)

    entries: list[dict[str, object]] = []
    for ticker in sorted(pool):
        nominations = sorted(
            nominations_by_ticker[ticker],
            key=lambda item: APPROACH_IDS.index(item.valuation_approach_id),
        )
        row = by_ticker[ticker]
        entries.append(
            ReviewSetEntry(
                ticker=ticker,
                name=string_or_none(row.get("name")) or "",
                sector_33=string_or_none(row.get("sector_33")) or "",
                nominations=tuple(nominations),
                analysis=ReviewSetAnalysis.model_validate(
                    _analysis(row, normalized_gap=normalized_gaps.get(ticker))
                ),
            ).model_dump(mode="json")
        )
    return {
        "schema_version": 2,
        "kind": "review_set",
        "method": {
            "method_id": rules.method_id,
            "method_hash": candidate_discovery_method_hash(
                rules,
                required_jpx_flags=required_jpx_flags,
            ),
            "nomination_depth": rules.nomination_depth,
        },
        "entries": entries,
        "diagnostics": {
            "common_eligible_count": len(eligible),
            "nomination_counts": nomination_counts,
            "unique_candidate_count": len(pool),
            "review_set_count": len(entries),
        },
    }


def build_nomination_ranks(
    security_analyses: Sequence[Mapping[str, object]],
    *,
    rules: CandidateDiscoveryRules,
    required_jpx_flags: Sequence[str],
) -> dict[str, tuple[Nomination, ...]]:
    """Return every approach nomination used by the union and calibration replay."""

    eligible = _eligible_population(security_analyses, rules, required_jpx_flags)
    normalized_gaps = normalized_earnings_sector_gaps(_normalized_earnings_inputs(eligible))
    return _nomination_ranks(eligible, rules=rules, normalized_gaps=normalized_gaps)


def _eligible_population(
    rows: Sequence[Mapping[str, object]],
    rules: CandidateDiscoveryRules,
    required_jpx_flags: Sequence[str],
) -> list[Mapping[str, object]]:
    return [
        row for row in rows if _common_eligible(row, rules, required_jpx_flags=required_jpx_flags)
    ]


def _nomination_ranks(
    eligible: Sequence[Mapping[str, object]],
    *,
    rules: CandidateDiscoveryRules,
    normalized_gaps: Mapping[str, float | None],
) -> dict[str, tuple[Nomination, ...]]:
    reinvestment_floors = _reinvestment_quality_floors(eligible)
    ordered = {
        approach: _ordered_eligible(
            approach,
            eligible,
            normalized_gaps=normalized_gaps,
            reinvestment_floors=reinvestment_floors,
        )
        for approach in APPROACH_IDS
    }
    nominations_by_ticker: dict[str, list[Nomination]] = defaultdict(list)
    for approach in APPROACH_IDS:
        method_id = rules.approaches[approach].method_id
        # 有限の調査入力枠であり、枠外を経済的rejectへ変換しない (#1481)。
        # docs/reference/screening-runtime.md#nomination-capacity
        for rank, row in enumerate(ordered[approach][: rules.nomination_depth], start=1):
            nominations_by_ticker[_ticker(row)].append(
                Nomination(
                    valuation_approach_id=approach,
                    valuation_method_id=method_id,
                    rank=rank,
                )
            )
    return {
        ticker: tuple(
            sorted(
                nominations,
                key=lambda item: APPROACH_IDS.index(item.valuation_approach_id),
            )
        )
        for ticker, nominations in nominations_by_ticker.items()
    }


def validate_review_set_payload(
    payload: Mapping[str, object],
    *,
    security_analyses: Sequence[Mapping[str, object]],
    rules: CandidateDiscoveryRules,
    required_jpx_flags: Sequence[str],
) -> None:
    validate_review_set_shape(payload)
    expected = build_review_set(
        security_analyses,
        rules=rules,
        required_jpx_flags=required_jpx_flags,
    )
    comparable = {key: payload.get(key) for key in expected}
    if canonical_json(comparable) != canonical_json(expected):
        raise ReviewSetContractError("review set does not match its source analyses and method")


def validate_review_set_shape(payload: Mapping[str, object]) -> None:
    try:
        PublishedReviewSet.model_validate(payload)
    except ValueError as error:
        raise ReviewSetContractError(f"published review set is invalid: {error}") from error


def _common_eligible(
    row: Mapping[str, object],
    rules: CandidateDiscoveryRules,
    *,
    required_jpx_flags: Sequence[str],
) -> bool:
    required_flags = frozenset(required_jpx_flags)
    flags = row.get("jpx_flags")
    jpx_flags = tuple(str(value) for value in flags) if isinstance(flags, list | tuple) else None
    return rules.common_eligibility.matches(
        market_cap_oku=optional_float(row.get("market_cap_oku")),
        listing_span_days=optional_float(row.get("listing_span_days")),
        jpx_flags=jpx_flags,
        required_jpx_flags=required_flags,
        require_facts=True,
    )


def _ordered_eligible(
    approach: str,
    rows: Sequence[Mapping[str, object]],
    *,
    normalized_gaps: Mapping[str, float | None],
    reinvestment_floors: Mapping[str, _QualityFloors],
) -> list[Mapping[str, object]]:
    eligible: list[tuple[tuple[object, ...], Mapping[str, object]]] = []
    for row in rows:
        metrics = metric_map(row.get("metrics"))
        ticker = _ticker(row)
        key: tuple[object, ...]
        if approach == "current-earnings-power":
            chosen = _positive(row.get("per_forward")) or _positive(row.get("per_trailing"))
            gap_key = (
                "per_forward_sector_gap"
                if _positive(row.get("per_forward"))
                else "per_trailing_sector_gap"
            )
            gap = _finite(metrics.get(gap_key))
            if gap is None:
                gap = _finite(metrics.get("chosen_per_sector_gap"))
            if chosen is None or gap is None:
                continue
            key = (
                gap,
                chosen,
                _desc_null(metrics.get("fcf_yield")),
                _desc_null(metrics.get("ocf_yield")),
                _asc_null(row.get("ev_ebitda")),
                ticker,
            )
        elif approach == "normalized-earnings-power":
            normalized = _positive(metrics.get("normalized_per_3fy"))
            gap = normalized_gaps.get(ticker)
            if normalized is None or gap is None:
                continue
            key = (
                gap,
                normalized,
                _desc_null(metrics.get("fcf_yield")),
                _asc_null(row.get("per_trailing")),
                ticker,
            )
        elif approach == "asset-value":
            if string_or_none(row.get("sector_33")) in _ASSET_VALUE_EXCLUDED_SECTORS:
                continue
            asset = _positive(metrics.get("asset_backed_ratio"))
            if asset is None:
                continue
            key = (
                -asset,
                _asc_null(metrics.get("pbr_sector_gap")),
                _asc_null(row.get("pbr")),
                _desc_null(metrics.get("equity_ratio")),
                ticker,
            )
        else:
            values = _reinvestment_values(row)
            floors = reinvestment_floors.get(ticker)
            if values is None or floors is None:
                continue
            p_s_gap, capital_return, sales_yoy, operating_margin, fcf_yield = values
            capital_return_floor, operating_margin_floor = floors
            if (
                p_s_gap >= 0
                or capital_return < capital_return_floor
                or operating_margin < operating_margin_floor
            ):
                continue
            key = (p_s_gap, -capital_return, -sales_yoy, -operating_margin, -fcf_yield, ticker)
        eligible.append((key, row))
    eligible.sort(key=lambda item: item[0])
    return [row for _, row in eligible]


def _normalized_earnings_inputs(
    rows: Sequence[Mapping[str, object]],
) -> list[tuple[str, str, float | None]]:
    return [
        (
            _ticker(row),
            string_or_none(row.get("sector_33")) or "",
            _positive(metric_map(row.get("metrics")).get("normalized_per_3fy")),
        )
        for row in rows
    ]


def _reinvestment_quality_floors(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, _QualityFloors]:
    """低収益性の成長を再投資価値と誤認しない相対quality境界 (#1153)。

    安いP/Sの順位だけではこの問いを守れない。収益優位を実証したfloorではない。
    docs/reference/screening-runtime.md#approach-applicability
    """
    observed: list[tuple[Mapping[str, object], _ReinvestmentValues]] = []
    by_sector: dict[str, list[_ReinvestmentValues]] = defaultdict(list)
    for row in rows:
        values = _reinvestment_values(row)
        if values is None:
            continue
        observed.append((row, values))
        by_sector[string_or_none(row.get("sector_33")) or ""].append(values)
    market = [values for _, values in observed]
    if not market:
        return {}
    market_floor = _quality_floor(market)
    floors = {
        sector: _quality_floor(values)
        if len(values) >= MIN_SECTOR_MEDIAN_POPULATION
        else market_floor
        for sector, values in by_sector.items()
    }
    return {
        _ticker(row): floors[string_or_none(row.get("sector_33")) or ""]
        for row, _values in observed
    }


def _quality_floor(values: Sequence[_ReinvestmentValues]) -> _QualityFloors:
    return _QualityFloors(
        median(value.capital_return for value in values),
        median(value.operating_margin for value in values),
    )


def _analysis(row: Mapping[str, object], *, normalized_gap: float | None) -> dict[str, object]:
    metrics = dict(metric_map(row.get("metrics")))
    reinvestment = _reinvestment_observations(row)
    quality = dict(metric_map(row.get("ttm_quality")))
    machine_valuation = machine_valuation_from_analysis(row)
    return {
        "identity_liquidity": {
            key: row.get(key)
            for key in ("market_cap_oku", "avg_turnover_oku", "listing_span_days", "jpx_flags")
        },
        "valuation": {
            key: row.get(key)
            for key in ("per_forward", "per_trailing", "pbr", "ev_ebitda", "p_s", "pcfr")
        },
        "current_earnings": {
            key: metrics.get(key)
            for key in (
                "fcf_yield",
                "ocf_yield",
                "forecast_special_gain_flag",
                "forecast_full_year_loss_flag",
            )
        },
        "normalized_earnings": {
            "normalized_per_3fy": metrics.get("normalized_per_3fy"),
            "normalized_per_3fy_sector_gap": normalized_gap,
        },
        "asset_value": {
            key: metrics.get(key)
            for key in (
                "asset_backed_ratio",
                "net_cash_to_market_cap",
                "investment_securities",
                "equity_ratio",
            )
        },
        "reinvestment": reinvestment.model_dump(mode="json"),
        "expected_return": {
            **{
                key: metrics.get(key)
                for key in (
                    "er_annual",
                    "er_reversion_annual",
                    "er_carry_annual",
                    "fv_sector_median_yen",
                    "fv_self_range_yen",
                    "er_origin",
                    "er_model_version",
                    "er_unit",
                    "er_assumptions",
                )
            },
            "dividend_yield": machine_valuation.dividend_yield,
            "dividend_basis": machine_valuation.dividend_basis,
            "net_share_change_yoy": machine_valuation.net_share_change_yoy,
            "carry_missing_assumption": machine_valuation.carry_missing_assumption,
        },
        "data_quality": {
            **{
                key: metrics.get(key)
                for key in (
                    "bs_carry_forward_fields",
                    "bs_carry_forward_lag_days",
                    "edinet_failure_reasons",
                    "stale_fin_flag",
                )
            },
            "ttm_quality_ev_ebitda": quality.get("ev_ebitda"),
            "ttm_quality_fcf": quality.get("fcf_yield"),
        },
        "context": {
            key: metrics.get(key)
            for key in (
                "next_earnings_status",
                "next_earnings_estimated_date",
                "margin_short_to_adv",
                "tse_capital_policy_status",
                "large_holding_event_recent",
                "tender_offer_event_recent",
            )
        },
    }


def _reinvestment_observations(row: Mapping[str, object]) -> ReinvestmentAnalysis:
    """Preserve known coordinates independently of the nomination requirements."""
    metrics = metric_map(row.get("metrics"))
    operating_profit = _finite(metrics.get("operating_profit_ttm"))
    sales = _positive(metrics.get("sales_ttm"))
    equity = _finite(metrics.get("same_state_equity_yen"))
    debt = _finite(metrics.get("debt"))
    cash = _finite(metrics.get("cash"))
    capital = (
        _positive(equity + debt - cash)
        if equity is not None and debt is not None and cash is not None
        else None
    )
    return ReinvestmentAnalysis(
        p_s_sector_gap=_finite(metrics.get("p_s_sector_gap")),
        operating_return_on_capital_proxy=(
            operating_profit / capital
            if operating_profit is not None and capital is not None
            else None
        ),
        sales_yoy=_finite(metrics.get("sales_yoy")),
        operating_margin=(
            operating_profit / sales if operating_profit is not None and sales is not None else None
        ),
        fcf_yield=_finite(metrics.get("fcf_yield")),
    )


def _reinvestment_values(
    row: Mapping[str, object],
) -> _ReinvestmentValues | None:
    metrics = metric_map(row.get("metrics"))
    p_s = _positive(row.get("p_s"))
    p_s_gap = _finite(metrics.get("p_s_sector_gap"))
    sales_yoy = _positive(metrics.get("sales_yoy"))
    fcf_yield = _finite(metrics.get("fcf_yield"))
    operating_profit_ttm = _positive(metrics.get("operating_profit_ttm"))
    sales_ttm = _positive(metrics.get("sales_ttm"))
    same_state_equity_yen = _positive(metrics.get("same_state_equity_yen"))
    debt = _finite(metrics.get("debt"))
    cash = _finite(metrics.get("cash"))
    values = (
        p_s,
        p_s_gap,
        sales_yoy,
        fcf_yield,
        operating_profit_ttm,
        sales_ttm,
        same_state_equity_yen,
        debt,
        cash,
    )
    if any(value is None for value in values):
        return None
    assert p_s_gap is not None
    assert sales_yoy is not None
    assert fcf_yield is not None
    observations = _reinvestment_observations(row)
    operating_margin = observations.operating_margin
    capital_return = observations.operating_return_on_capital_proxy
    if operating_margin is None or capital_return is None:
        return None
    if operating_margin <= 0 or capital_return <= 0:
        return None
    return _ReinvestmentValues(p_s_gap, capital_return, sales_yoy, operating_margin, fcf_yield)


def _ticker(row: Mapping[str, object]) -> str:
    ticker = string_or_none(row.get("ticker"))
    if ticker is None:
        raise ReviewSetContractError("security analysis has no ticker")
    return ticker


def _finite(value: object) -> float | None:
    parsed = optional_float(value)
    return parsed if parsed is not None and isfinite(parsed) else None


def _positive(value: object) -> float | None:
    parsed = _finite(value)
    return parsed if parsed is not None and parsed > 0 else None


def _asc_null(value: object) -> tuple[bool, float]:
    parsed = _finite(value)
    return parsed is None, parsed or 0.0


def _desc_null(value: object) -> tuple[bool, float]:
    parsed = _finite(value)
    return parsed is None, -(parsed or 0.0)


__all__ = [
    "ReviewSetContractError",
    "build_nomination_ranks",
    "build_review_set",
    "candidate_discovery_method_hash",
    "validate_review_set_payload",
    "validate_review_set_shape",
]
