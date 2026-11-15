"""通常生成の一社streamで発見根拠と財務選別を結ぶ。正式判断は作らない。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import cast

from pydantic import JsonValue, TypeAdapter

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationMetadata
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import (
    project_company_with_input_reviews,
)
from baibai_engine.machine_analysis.screening.policies.economic_paths import PathCoordinates
from baibai_engine.machine_analysis.screening.policies.financial_growth import (
    POLICY_ID as FINANCIAL_POLICY_ID,
)
from baibai_engine.machine_analysis.screening.policies.financial_growth import (
    evaluate_financial_growth_gate,
)
from baibai_engine.machine_analysis.screening.policies.technical_discovery import (
    STRATEGY_POLICY_ID,
    evaluate_technical_strategy,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialRow,
    TechnicalFinancialSelection,
    discovery_display_order,
    financial_screening_lane,
)
from baibai_engine.primitives.stocks import normalize_ticker


def technical_financial_selection_id(
    generation_id: str, *, policy: TechnicalFinancialPolicy
) -> str:
    return f"selection-{generation_id}-{policy.method}"


def project_technical_financial_row(
    ticker: str, *, models: tuple[GeneratedModel, ...], metadata: GenerationMetadata
) -> TechnicalFinancialRow:
    technical = evaluate_technical_strategy(ticker, models=models, metadata=metadata)
    # 財務による独立発見をTAの合否・欠測で止めない。
    gate = evaluate_financial_growth_gate(
        ticker,
        models=tuple(
            model for model in models if model.model_id in {"company_financials", "company_growth"}
        ),
        metadata=metadata,
    )
    coordinates, input_reviews = project_company_with_input_reviews(
        ticker, models=models, metadata=metadata
    )
    paths = TypeAdapter(tuple[PathCoordinates, ...]).dump_python(coordinates.paths, mode="json")
    financial_paths: dict[str, JsonValue] = {
        "source": "saved_model_economic_path_coordinates_not_ranking",
        "matched_routes": cast(
            list[JsonValue], sorted({path.path_id for path in coordinates.paths if path.eligible})
        ),
        "paths": cast(list[JsonValue], paths),
        "model_revisions": dict(coordinates.model_revisions),
        "not_generated": list(coordinates.not_generated),
    }
    # 元fieldが未記録なら、型の既定空配列を「変更なし」の観測へ昇格しない。
    if any(
        model.model_id == "company_financials"
        and "accounting_disclosures" in _object(model.payload.get("earnings_price"))
        for model in models
    ):
        financial_paths.update(_pick(coordinates.context, "accounting_change_context"))
    if (
        gate["state"] in {"not_selected", "input_unresolved"}
        and not financial_paths["matched_routes"]
        and input_reviews
    ):
        financial_paths["input_review_reasons"] = cast(list[JsonValue], list(input_reviews))
    # 状態/条件/不足は全社保存する。元の財務観測と価格時系列はGenerationが正本で、
    # 選定contextの明示詳細とResearch保存時に同じsubjectの完全モデルを読む。
    compact_technical = _pick(
        technical,
        "policy_id",
        "state",
        "matched_routes",
        "routes",
        "observations",
        "price_context",
        "expected_price_date",
        "market_relative_benchmark",
        "missing_models",
        "model_revisions",
    )
    # 相対的な活動条件だけでなく、同じ観測の円規模・窓・不足を初回比較へ残す。
    compact_technical.update(_pick(coordinates.context, "turnover_activity_20d"))
    compact_technical["observation_failures"] = {
        name: _pick(
            _object(raw),
            "source_field",
            "source_state",
            "unavailable_reason",
            "source_status",
            "source_unavailable_reason",
        )
        for name, raw in _object(technical.get("observations")).items()
        if _object(raw).get("unavailable_reason") is not None
    }
    return TechnicalFinancialRow(
        ticker=ticker,
        name=coordinates.name,
        sector=coordinates.sector,
        technical=compact_technical,
        financial_gate=_pick(
            gate,
            "policy_id",
            "state",
            "reason",
            "matched_routes",
            "routes",
            "checks",
            "missing_models",
            "model_revisions",
            "limits",
            "forecast_context",
            "cash_flow_context",
        ),
        financial_paths=financial_paths,
        screening_lane=financial_screening_lane(
            growth_state=cast(str, gate["state"]),
            technical_match=technical["state"] == "matched",
            financial_path_match=bool(financial_paths["matched_routes"]),
            financial_input_review=bool(financial_paths.get("input_review_reasons")),
        ),
    )


def evaluate_technical_financial_rows(
    metadata: GenerationMetadata,
    *,
    tickers: Sequence[str],
    rows: Mapping[str, TechnicalFinancialRow],
    policy: TechnicalFinancialPolicy,
) -> ScreeningSelection:
    if policy.method != "technical-financial-discovery-v11":
        raise ValueError("historical technical-financial policy is read-only")
    subjects = tuple(sorted(normalize_ticker(ticker) for ticker in tickers))
    if len(set(subjects)) != len(subjects) or not set(rows).issubset(subjects):
        raise ValueError("technical-financial screening requires unique planned companies")
    if metadata.subject_plan is not None and set(subjects) != {
        ticker for ticker, plan in metadata.subject_plan.items() if plan.scope == "stock"
    }:
        raise ValueError("screening universe differs from the saved stock plan")
    complete = tuple(
        rows[ticker]
        if ticker in rows
        else project_technical_financial_row(ticker, models=(), metadata=metadata)
        for ticker in subjects
    )
    if any(row.ticker != ticker for row, ticker in zip(complete, subjects, strict=True)):
        raise ValueError("screening row differs from its subject")
    technical_counts = Counter(str(row.technical["state"]) for row in complete)
    financial_counts = Counter(str(row.financial_gate["state"]) for row in complete)
    lane_counts = Counter(row.screening_lane for row in complete)
    order = discovery_display_order(complete)
    result = TechnicalFinancialSelection(
        policy=policy,
        technical_policy_id=STRATEGY_POLICY_ID,
        financial_policy_id=FINANCIAL_POLICY_ID,
        rows=complete,
        display_order=order,
        summary={
            "planned_stocks": len(complete),
            "technical": {
                state: technical_counts[state]
                for state in ("matched", "not_matched", "unavailable")
            },
            "financial_gate": {
                state: financial_counts[state]
                for state in (
                    "research_candidate",
                    "not_selected",
                    "input_unresolved",
                    "not_evaluated",
                )
            },
            "display_pool_count": len(order),
            "screening_lanes": {
                lane: lane_counts[lane]
                for lane in (
                    "growth_research",
                    "input_review",
                    "alternative_financial_research",
                    "no_current_hypothesis",
                )
            },
        },
    )
    return ScreeningSelection(metadata.generation_id, subjects, result, _price_inputs(complete))


def _price_inputs(rows: Sequence[TechnicalFinancialRow]) -> dict[str, JsonValue]:
    dates = Counter(
        str(
            _object(row.technical.get("price_context")).get("latest_observed_price_date")
            or "missing"
        )
        for row in rows
    )
    expected = Counter(str(row.technical.get("expected_price_date") or "missing") for row in rows)
    valid = sum(_object(row.technical.get("price_context")).get("ready") is True for row in rows)
    stale = sum(
        _object(row.technical.get("price_context")).get("latest_observed_price_date") is not None
        and row.technical.get("expected_price_date") is not None
        and _object(row.technical.get("price_context"))["latest_observed_price_date"]
        != row.technical["expected_price_date"]
        for row in rows
    )
    return {
        "basis": "technical_price_path_readiness_not_all_pricing",
        "universe_count": len(rows),
        "valid_count": valid,
        "date_mismatch_count": stale,
        "invalid_count": len(rows) - valid,
        "price_dates": dict(sorted(dates.items())),
        "expected_price_dates": dict(sorted(expected.items())),
    }


def _object(value: JsonValue | None) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _pick(value: dict[str, JsonValue], *keys: str) -> dict[str, JsonValue]:
    return {key: deepcopy(value[key]) for key in keys if key in value}
