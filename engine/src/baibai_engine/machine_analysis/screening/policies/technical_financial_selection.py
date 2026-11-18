"""通常生成の一社streamで発見根拠と財務選別を結ぶ。正式判断は作らない。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import cast

from pydantic import JsonValue, TypeAdapter

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, GenerationMetadata
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.discovery import read_discovery_observations
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    PathCoordinates,
)
from baibai_engine.machine_analysis.screening.policies.financial_growth import (
    POLICY_ID as FINANCIAL_POLICY_ID,
)
from baibai_engine.machine_analysis.screening.policies.technical_discovery import (
    STRATEGY_POLICY_ID,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialRow,
    TechnicalFinancialSelection,
    discovery_display_order,
    financial_screening_lane,
)
from baibai_engine.primitives.stocks import normalize_ticker

_PATH_CODEC = TypeAdapter(tuple[PathCoordinates, ...])


def technical_financial_selection_id(
    generation_id: str, *, policy: TechnicalFinancialPolicy
) -> str:
    return f"selection-{generation_id}-{policy.method}"


def project_technical_financial_row(
    ticker: str, *, models: tuple[GeneratedModel, ...], metadata: GenerationMetadata
) -> TechnicalFinancialRow:
    technical, gate, coordinates, input_reviews = read_discovery_observations(
        ticker, models=models, metadata=metadata
    )
    paths = _PATH_CODEC.dump_python(coordinates.paths, mode="json")
    financial_paths: dict[str, JsonValue] = {
        "source": "saved_model_economic_path_coordinates_not_ranking",
        "matched_routes": cast(
            list[JsonValue], sorted({path.path_id for path in coordinates.paths if path.eligible})
        ),
        "paths": cast(list[JsonValue], paths),
        "model_revisions": dict(coordinates.model_revisions),
        "not_generated": list(coordinates.not_generated),
    }
    # PBRの分母を、別sourceの純資産や現在株数の測定日で補い直さない。
    financial_paths.update(_pick(_object(coordinates.context.get("financial")), "book_equity"))
    # 元fieldが未記録なら、型の既定空配列を「変更なし」の観測へ昇格しない。
    if any(
        model.model_id == "company_financials"
        and "accounting_disclosures" in _object(model.payload.get("earnings_price"))
        for model in models
    ):
        financial_paths.update(_pick(coordinates.context, "accounting_change_context"))
    # 利益・価格・会計basisの不足も含む元理由を、資本だけの不成立と呼ばない。
    for model in models:
        if model.model_id != "company_financials":
            continue
        earnings = _object(model.payload.get("earnings_price"))
        retained = {
            target: original[source]
            for source, target, original in (
                ("capital_action_evidence", "observations", model.payload),
                ("share_basis_adjustments", "share_basis_adjustments", earnings),
                ("unavailable_reason", "earnings_price_unavailable_reason", earnings),
            )
            if source in original
        }
        if retained:
            financial_paths["capital_action_context"] = deepcopy(retained)
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
    compact_technical["price_path_60d"] = _price_path_brief(
        _object(_object(technical.get("supporting_context")).get("price_path_60d"))
    )
    compact_technical["market_conditioned_60d"] = _market_conditioned_brief(
        _object(_object(technical.get("supporting_context")).get("market_conditioned_60d"))
    )
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
    definition = None if metadata.subject_plan is None else metadata.subject_plan.get(ticker)
    # 関連の理由を調査対象の所属や全社曝露へ広げず、固定planのまま渡す。
    related_context = (
        definition.model_dump(mode="json", include={"related_subjects", "related_subject_basis"})
        if definition is not None and definition.related_subjects
        else None
    )
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
            "observation_period_context",
            "quarter_growth_context",
            "income_attribution_context",
            "reported_share_context",
            "reported_balance_context",
            "dividend_forecast_context",
        ),
        financial_paths=financial_paths,
        screening_lane=financial_screening_lane(
            growth_state=cast(str, gate["state"]),
            technical_match=technical["state"] == "matched",
            financial_path_match=bool(financial_paths["matched_routes"]),
            financial_input_review=bool(financial_paths.get("input_review_reasons")),
        ),
        related_subject_context=related_context,
    )


def evaluate_technical_financial_rows(
    metadata: GenerationMetadata,
    *,
    tickers: Sequence[str],
    rows: Mapping[str, TechnicalFinancialRow],
    policy: TechnicalFinancialPolicy,
) -> ScreeningSelection:
    if policy.method != "technical-financial-discovery-v26":
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


def _market_conditioned_brief(section: dict[str, JsonValue]) -> dict[str, JsonValue]:
    result = _pick(section, "source_field", "source_state")
    value = section.get("value")
    if not isinstance(value, dict):
        return {**result, **_pick(section, "value")}
    # 既定代理の探索理由を市場全体の耐性へ外挿せず、各代理の日集合を併読する。
    compact = _pick(
        value,
        "window_basis",
        "calendar_differences",
        "window_start_date",
        "window_end_date",
        "price_basis",
    )
    benchmarks = value.get("benchmarks")
    if isinstance(benchmarks, dict):
        compact["benchmarks"] = {
            key: _pick(
                item,
                "benchmark_kind",
                "status",
                "unavailable_reason",
                "own_valid_close_count",
                "benchmark_valid_close_count",
                "flat_market_days",
                "up",
                "down",
            )
            if isinstance(item, dict)
            else deepcopy(item)
            for key, item in benchmarks.items()
        }
    else:
        compact.update(_pick(value, "benchmarks"))
    result["value"] = compact
    result["interpretation"] = (
        "ETF代理ごとの上昇/下落日を別々に数えるため、比較する日集合は異なる場合があります。"
        "own_up_daysは自社return>0、outperform_daysは自社return>当該代理returnの日数です。"
        "条件付き一日平均は累積収益や将来勝率ではなく、二つの代理を独立した支持票にしません。"
    )
    return result


def _price_path_brief(section: dict[str, JsonValue]) -> dict[str, JsonValue]:
    result = _pick(section, "source_field", "source_state")
    value = section.get("value")
    if not isinstance(value, dict):
        return {**result, **_pick(section, "value")}
    # 端点の上昇と途中の下落/一日集中を並べる。分析用価格を注文値にしない。
    result["value"] = {
        **_pick(
            value,
            "kind",
            "calendar_differences",
            "window_start_date",
            "window_end_date",
            "value",
            "unavailable_reason",
            "observed_peak_date",
            "window_return",
            "window_return_unavailable_reason",
        ),
        **(
            {
                "path_statistics": _pick(
                    _object(value["path_statistics"]),
                    "status",
                    "price_row_basis",
                    "max_close_drawdown",
                    "log_path_efficiency",
                    "efficiency_unavailable_reason",
                    "largest_absolute_log_change_share",
                )
            }
            if isinstance(value.get("path_statistics"), dict)
            else _pick(value, "path_statistics")
        ),
        **(
            {
                "coverage": _pick(
                    _object(value["coverage"]),
                    "calendar_status",
                    "expected_dates",
                    "row_counts",
                    "complete",
                )
            }
            if isinstance(value.get("coverage"), dict)
            else _pick(value, "coverage")
        ),
    }
    return result


def _object(value: JsonValue | None) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _pick(value: dict[str, JsonValue], *keys: str) -> dict[str, JsonValue]:
    return {key: deepcopy(value[key]) for key in keys if key in value}
