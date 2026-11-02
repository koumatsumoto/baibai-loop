"""保存世代を同一断面で読み、経済経路の候補政策へ渡す。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.query import stream_generation_subjects
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


def evaluate_saved_economic_generation(
    generation_id: str,
    *,
    tickers: Sequence[str],
    policy: EconomicPathPolicy,
    path: Path | None = None,
) -> EconomicSelectionEvaluation:
    """全社を一つの読取断面で走査し、個社payloadを順に小さな座標へ縮める。"""
    subjects = tuple(sorted(normalize_ticker(ticker) for ticker in tickers))
    if len(set(subjects)) != len(subjects):
        raise ValueError("screening universe must contain unique companies")
    with stream_generation_subjects(generation_id, subjects=subjects, path=path) as generation:
        if generation is None:
            raise ValueError("model generation not found")
        metadata, companies = generation
        coordinates: dict[str, CompanyCoordinates] = {}
        for subject, models in companies:
            coordinates[subject] = project_company(subject, models=models, metadata=metadata)
            # 次社の復元前に、この社のpayload参照を手放す。
            del models
        for subject in subjects:
            if subject not in coordinates:
                coordinates[subject] = project_company(subject, models=(), metadata=metadata)
    selection = ScreeningSelection(
        generation_id=generation_id,
        universe_tickers=subjects,
        result=select_economic_paths(tuple(coordinates[t] for t in subjects), policy=policy),
    )
    return EconomicSelectionEvaluation(
        selection=selection, price_inputs=_price_inputs(coordinates.values())
    )


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


def select_saved_economic_generation(
    generation_id: str,
    *,
    tickers: Sequence[str],
    policy: EconomicPathPolicy,
    path: Path | None = None,
) -> ScreeningSelection:
    """候補だけが必要な読取。診断も同じ一回の座標投影で計算する。"""
    return evaluate_saved_economic_generation(
        generation_id, tickers=tickers, policy=policy, path=path
    ).selection
