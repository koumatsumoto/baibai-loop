"""固定世代の座標を経済経路の候補政策へ渡す。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import GenerationMetadata
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.economic_path_inputs import project_company
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CompanyCoordinates,
    EconomicPathPolicy,
    select_economic_paths,
)
from baibai_engine.primitives.stocks import normalize_ticker


def economic_selection_id(generation_id: str, *, policy: EconomicPathPolicy) -> str:
    """新規候補のidentityを固定世代と政策methodに結びつける。"""
    return f"selection-{generation_id}-{policy.method}"


def evaluate_economic_coordinates(
    metadata: GenerationMetadata,
    *,
    tickers: Sequence[str],
    coordinates: Mapping[str, CompanyCoordinates],
    policy: EconomicPathPolicy,
) -> EconomicSelectionEvaluation:
    """同じ固定世代の全社座標で選ぶ。未生成企業も比較対象集合から落とさない。"""
    subjects = _universe(tickers)
    companies = tuple(
        coordinates[subject]
        if subject in coordinates
        else project_company(subject, models=(), metadata=metadata)
        for subject in subjects
    )
    selection = ScreeningSelection(
        generation_id=metadata.generation_id,
        universe_tickers=subjects,
        result=select_economic_paths(companies, policy=policy),
    )
    return EconomicSelectionEvaluation(selection=selection, price_inputs=_price_inputs(companies))


def _universe(tickers: Sequence[str]) -> tuple[str, ...]:
    subjects = tuple(sorted(normalize_ticker(ticker) for ticker in tickers))
    if len(set(subjects)) != len(subjects):
        raise ValueError("screening universe must contain unique companies")
    return subjects


@dataclass(frozen=True, slots=True)
class EconomicSelectionEvaluation:
    selection: ScreeningSelection
    price_inputs: dict[str, JsonValue]


def _price_inputs(companies: Iterable[CompanyCoordinates]) -> dict[str, JsonValue]:
    """候補0を投資機会なしへ読み替えないため、選定時と同じ座標の入力状態を示す。"""
    rows = tuple(companies)
    dates = Counter(str(row.context.get("price_date") or "missing") for row in rows)
    expected = Counter(str(row.context.get("expected_price_date") or "missing") for row in rows)
    valid = sum(row.context.get("pricing_valid") is True for row in rows)
    stale = sum(
        row.context.get("price_date") is not None
        and row.context.get("expected_price_date") is not None
        and row.context["price_date"] != row.context["expected_price_date"]
        for row in rows
    )
    return {
        "universe_count": len(rows),
        "valid_count": valid,
        "date_mismatch_count": stale,
        "invalid_count": len(rows) - valid,
        "price_dates": dict(sorted(dates.items())),
        "expected_price_dates": dict(sorted(expected.items())),
    }
