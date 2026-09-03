"""Produce the exact nomination union consumed by Research Triage."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from hashlib import sha256
from math import isfinite
from statistics import median
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.appdb.json import canonical_json
from baibai_engine.foundation.candidate_discovery import (
    Nomination,
    ReviewSetAnalysis,
    ReviewSetEntry,
    ReviewSetMethod,
)
from baibai_engine.foundation.coerce import metric_map, optional_float, string_or_none
from baibai_engine.screening.metrics import MIN_SECTOR_MEDIAN_POPULATION
from baibai_engine.screening.rule_config import CandidateDiscoveryRules

APPROACH_IDS = (
    "current-earnings-power",
    "normalized-earnings-power",
    "asset-value",
    "reinvestment-value",
)
_ASSET_VALUE_EXCLUDED_SECTORS = frozenset(
    {
        "銀行業",
        "保険業",
        "その他金融業",
        "証券・商品先物取引業",
    }
)


class ReviewSetContractError(ValueError):
    """Raised when a Review Set cannot be reproduced from its source analyses."""


class PublishedReviewSet(BaseModel):
    """Typed L2 publication rebuilt only from a screening run and its rules."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2]
    kind: Literal["review_set"]
    review_set_id: str = Field(min_length=1)
    run_revision_id: str = Field(min_length=1)
    as_of: date
    created_at: datetime
    screening_rules_hash: str = Field(min_length=1)
    method: ReviewSetMethod
    entries: tuple[ReviewSetEntry, ...]
    diagnostics: Mapping[str, object]

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_as_of(cls, value: object) -> object:
        return date.fromisoformat(value) if isinstance(value, str) else value

    @field_validator("created_at", mode="before")
    @classmethod
    def _parse_created_at(cls, value: object) -> object:
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @field_validator("entries", mode="before")
    @classmethod
    def _tuple_entries(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _validate_publication(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        tickers = [entry.ticker for entry in self.entries]
        if tickers != sorted(tickers) or len(tickers) != len(set(tickers)):
            raise ValueError("review set entries must be unique and ordered by ticker")
        if len(self.entries) > len(APPROACH_IDS) * self.method.nomination_depth:
            raise ValueError("review set exceeds the four-Approach Nomination union bound")
        ranks_by_approach: dict[str, list[int]] = {approach: [] for approach in APPROACH_IDS}
        for entry in self.entries:
            for nomination in entry.nominations:
                if nomination.valuation_approach_id not in ranks_by_approach:
                    raise ValueError("review set contains an unknown valuation Approach")
                ranks_by_approach[nomination.valuation_approach_id].append(nomination.rank)
        for approach, ranks in ranks_by_approach.items():
            if sorted(ranks) != list(range(1, len(ranks) + 1)):
                raise ValueError(f"{approach} nomination ranks must be contiguous from 1")
            if len(ranks) > self.method.nomination_depth:
                raise ValueError(f"{approach} exceeds nomination_depth")
        return self


def candidate_discovery_method_hash(
    rules: CandidateDiscoveryRules,
    *,
    required_jpx_flags: Sequence[str],
) -> str:
    representation = {
        "method_id": rules.method_id,
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
                    "fcf_yield",
                    "operating_profit",
                    "sales_ttm",
                    "total_assets",
                    "equity_ratio",
                ],
                "finite_inputs": ["p_s_sector_gap", "debt", "cash"],
                "p_s_sector_gap": "negative_required",
                "operating_margin": "sector_median_or_market_fallback_inclusive",
                "operating_return_on_capital_proxy": ("sector_median_or_market_fallback_inclusive"),
                "median_population": "common_eligible_with_positive_reinvestment_inputs",
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
    eligible = [
        row
        for row in security_analyses
        if _common_eligible(row, rules, required_jpx_flags=required_jpx_flags)
    ]
    normalized_gaps = _normalized_sector_gaps(eligible)
    nominations_by_ticker = {
        ticker: list(nominations)
        for ticker, nominations in build_nomination_ranks(
            security_analyses,
            rules=rules,
            required_jpx_flags=required_jpx_flags,
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

    eligible = [
        row
        for row in security_analyses
        if _common_eligible(row, rules, required_jpx_flags=required_jpx_flags)
    ]
    normalized_gaps = _normalized_sector_gaps(eligible)
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
    reinvestment_floors: Mapping[str, tuple[float, float]],
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


def _normalized_sector_gaps(rows: Sequence[Mapping[str, object]]) -> dict[str, float | None]:
    by_sector: dict[str, list[float]] = defaultdict(list)
    market: list[float] = []
    for row in rows:
        value = _positive(metric_map(row.get("metrics")).get("normalized_per_3fy"))
        if value is not None:
            market.append(value)
            by_sector[string_or_none(row.get("sector_33")) or ""].append(value)
    output: dict[str, float | None] = {}
    for row in rows:
        ticker = _ticker(row)
        value = _positive(metric_map(row.get("metrics")).get("normalized_per_3fy"))
        sector_values = by_sector[string_or_none(row.get("sector_33")) or ""]
        baseline = sector_values if len(sector_values) >= MIN_SECTOR_MEDIAN_POPULATION else market
        center = median(baseline) if baseline else None
        output[ticker] = (
            value / center - 1 if value is not None and center not in (None, 0) else None
        )
    return output


def _reinvestment_quality_floors(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, tuple[float, float]]:
    """Return inclusive quality floors from the existing reinvestment cross-section."""
    observed: list[tuple[Mapping[str, object], tuple[float, float, float, float, float]]] = []
    by_sector: dict[str, list[tuple[float, float, float, float, float]]] = defaultdict(list)
    for row in rows:
        values = _reinvestment_values(row)
        if values is None:
            continue
        observed.append((row, values))
        by_sector[string_or_none(row.get("sector_33")) or ""].append(values)
    market = [values for _, values in observed]
    output: dict[str, tuple[float, float]] = {}
    for row, _values in observed:
        sector_values = by_sector[string_or_none(row.get("sector_33")) or ""]
        baseline = sector_values if len(sector_values) >= MIN_SECTOR_MEDIAN_POPULATION else market
        output[_ticker(row)] = (
            median(value[1] for value in baseline),
            median(value[3] for value in baseline),
        )
    return output


def _analysis(row: Mapping[str, object], *, normalized_gap: float | None) -> dict[str, object]:
    metrics = dict(metric_map(row.get("metrics")))
    reinvestment = _reinvestment_values(row)
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
        "reinvestment": None
        if reinvestment is None
        else {
            "p_s_sector_gap": reinvestment[0],
            "operating_return_on_capital_proxy": reinvestment[1],
            "sales_yoy": reinvestment[2],
            "operating_margin": reinvestment[3],
            "fcf_yield": reinvestment[4],
        },
        "expected_return": {
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
        "data_quality": {
            key: metrics.get(key)
            for key in (
                "bs_carry_forward_fields",
                "bs_carry_forward_lag_days",
                "edinet_failure_reasons",
                "stale_fin_flag",
            )
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


def _reinvestment_values(
    row: Mapping[str, object],
) -> tuple[float, float, float, float, float] | None:
    metrics = metric_map(row.get("metrics"))
    p_s = _positive(row.get("p_s"))
    p_s_gap = _finite(metrics.get("p_s_sector_gap"))
    sales_yoy = _positive(metrics.get("sales_yoy"))
    fcf_yield = _positive(metrics.get("fcf_yield"))
    operating_profit = _positive(metrics.get("operating_profit"))
    sales_ttm = _positive(metrics.get("sales_ttm"))
    total_assets = _positive(metrics.get("total_assets"))
    equity_ratio = _positive(metrics.get("equity_ratio"))
    debt = _finite(metrics.get("debt"))
    cash = _finite(metrics.get("cash"))
    values = (
        p_s,
        p_s_gap,
        sales_yoy,
        fcf_yield,
        operating_profit,
        sales_ttm,
        total_assets,
        equity_ratio,
        debt,
        cash,
    )
    if any(value is None for value in values):
        return None
    assert p_s_gap is not None
    assert sales_yoy is not None
    assert fcf_yield is not None
    assert operating_profit is not None
    assert sales_ttm is not None
    assert total_assets is not None
    assert equity_ratio is not None
    assert debt is not None
    assert cash is not None
    operating_margin = operating_profit / sales_ttm
    invested_capital = total_assets * equity_ratio + debt - cash
    if invested_capital <= 0:
        return None
    capital_return = operating_profit / invested_capital
    if operating_margin <= 0 or capital_return <= 0:
        return None
    return p_s_gap, capital_return, sales_yoy, operating_margin, fcf_yield


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
    "APPROACH_IDS",
    "Nomination",
    "PublishedReviewSet",
    "ReviewSetAnalysis",
    "ReviewSetContractError",
    "ReviewSetEntry",
    "build_nomination_ranks",
    "build_review_set",
    "candidate_discovery_method_hash",
    "validate_review_set_payload",
    "validate_review_set_shape",
]
