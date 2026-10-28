"""固定したモデル生成をL3へ渡す。再計算・旧ERへのfallback・候補再選定はしない。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from baibai_engine.machine_analysis.generation.query import read_model_inputs
from baibai_engine.machine_analysis.generation.contracts import ModelInputSlice

from baibai_engine.investment_judgment.workflow.company_growth_projection import project_company_growth

_COMPANY_MODELS = (
    "company_valuation",
    "company_growth",
    "technical_analysis",
    "external_assessment",
    "market_context",
)


def verify_generated_inputs(saved: ModelInputSlice, *, runs_path: Path) -> None:
    canonical = read_model_inputs(saved.generation_id, subjects=saved.subjects, path=runs_path)
    if canonical != saved:
        raise ValueError("model input slice differs from its canonical generation")


def project_generated_inputs(
    saved: ModelInputSlice, *, company_growth_periods: int | None = 12
) -> dict[str, JsonValue]:
    """モデル固有の意味を維持し、未生成と生成内の欠測を区別して提示する。"""
    securities: dict[str, object] = {}
    for ticker in saved.subjects:
        if ticker == "market":
            continue
        models: dict[str, JsonValue] = {}
        revisions: dict[str, str] = {}
        for index, model in enumerate(saved.models):
            if model.subject != ticker:
                continue
            payload = deepcopy(model.payload)
            if model.model_id == "company_growth":
                payload = project_company_growth(
                    payload, ticker=ticker, periods=company_growth_periods
                )
                payload["full_result_ref"] = f"/inputs/generation/models/{index}/payload"
            models[model.model_id] = payload
            revisions[model.model_id] = model.calculation_revision
        securities[ticker] = {
            **models,
            "calculation_revisions": revisions,
            "not_generated": [name for name in _COMPANY_MODELS if name not in models],
        }
    common = saved.result("market_context", "market")
    return cast(
        dict[str, JsonValue],
        {
            "analysis_source": saved.model_dump(mode="json", exclude={"models"}),
            "market_context": None if common is None else deepcopy(common.payload),
            "market_context_calculation_revision": None
            if common is None
            else common.calculation_revision,
            "securities": securities,
        },
    )
