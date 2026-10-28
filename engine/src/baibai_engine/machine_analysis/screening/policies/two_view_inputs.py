"""明示したモデル観測を候補政策へ渡す。未知モデルの追加で選好を変えない。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.contracts import GeneratedModel, ModelGeneration
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.two_views import (
    ScreeningCoordinates,
    TwoViewPolicy,
    ViewSelection,
    select_views,
)
from baibai_engine.primitives.stocks import normalize_ticker


def select_generation(
    generation: ModelGeneration, *, tickers: Sequence[str], policy: TwoViewPolicy
) -> ScreeningSelection:
    """企業集合と政策を明示し、生成済み観測の意味や保存値を変更せず選ぶ。"""
    if not isinstance(generation, ModelGeneration):
        raise TypeError("screening requires a complete model generation")
    return _select_models(
        generation.generation_id, generation.models, tickers=tickers, policy=policy
    )


def _select_models(
    generation_id: str,
    models: Iterable[GeneratedModel],
    *,
    tickers: Sequence[str],
    policy: TwoViewPolicy,
) -> ScreeningSelection:
    subjects = sorted(normalize_ticker(ticker) for ticker in tickers)
    index = {(model.model_id, model.subject): model for model in models}
    coordinates = [
        ScreeningCoordinates.model_validate(
            {
                "ticker": ticker,
                "earnings_yield": _value(
                    index.get(("company_valuation", ticker)), "earnings_price", "earnings_yield"
                ),
                "price_momentum": _value(
                    index.get(("technical_analysis", ticker)), "price_momentum", "value"
                ),
            }
        )
        for ticker in subjects
    ]
    return ScreeningSelection(
        generation_id, tuple(subjects), select_views(coordinates, policy=policy)
    )


def _value(model: GeneratedModel | None, component: str, field: str) -> JsonValue:
    if model is None:
        return None
    observation = model.payload.get(component)
    if not isinstance(observation, dict) or field not in observation:
        raise ValueError(f"{model.model_id} requires {component}.{field}")
    return observation[field]
