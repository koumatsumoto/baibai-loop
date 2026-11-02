"""固定したモデル生成をL3へ渡す。再計算・旧ERへのfallback・候補再選定はしない。"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from baibai_engine.investment_judgment.workflow.company_growth_projection import (
    project_company_growth,
)
from baibai_engine.machine_analysis.generation.contracts import ModelInputSlice
from baibai_engine.machine_analysis.generation.query import read_research_model_inputs
from baibai_engine.primitives.json import canonical_json

_LEGACY_COMPANY_MODELS = (
    "company_financials",
    "company_growth",
    "technical_analysis",
    "external_assessment",
    "market_context",
)


def _project_market_context(
    source: dict[str, JsonValue], *, full_result_ref: str
) -> dict[str, JsonValue]:
    """Share repeated reading semantics only in the L3 display; saved input stays exact."""
    payload = deepcopy(source)
    reading = payload.get("reading")
    if not isinstance(reading, dict):
        return payload
    common, rows = reading.get("read_semantics"), reading.get("series")
    if not isinstance(common, dict) or not common or not isinstance(rows, list) or not rows:
        return payload
    # Do not invent inherited keys for an older or partial model. Every row must
    # already carry every common key, including explicit false/null values.
    semantics = []
    for row in rows:
        if not isinstance(row, dict):
            return payload
        own = row.get("read_semantics")
        if not isinstance(own, dict) or not common.keys() <= own.keys():
            return payload
        semantics.append((row, own))
    for row, own in semantics:
        row["read_semantics"] = {
            key: value
            for key, value in own.items()
            if key not in common or canonical_json(value) != canonical_json(common[key])
        }
    reading["read_semantics_inheritance"] = (
        "Each series inherits reading.read_semantics; its read_semantics keys override."
    )
    payload["full_result_ref"] = full_result_ref
    return payload


def verify_generated_inputs(
    saved: ModelInputSlice, *, targets: Sequence[str], runs_path: Path
) -> None:
    # Resolve required shared scope from the complete canonical header, never from
    # an input slice that could already have dropped a common market model.
    canonical = read_research_model_inputs(saved.generation_id, targets=targets, path=runs_path)
    if canonical != saved:
        raise ValueError("model input slice differs from its canonical generation")


def project_generated_inputs(
    saved: ModelInputSlice, *, company_growth_periods: int | None = 12
) -> dict[str, JsonValue]:
    """モデル固有の意味を維持し、未生成と生成内の欠測を区別して提示する。"""
    securities: dict[str, object] = {}
    shared_subjects: dict[str, object] = {}
    plan = saved.subject_plan
    for ticker in saved.subjects:
        if plan is None and ticker == "market":
            continue
        definition = None if plan is None else plan.get(ticker)
        scope = "stock" if definition is None else definition.scope
        models: dict[str, JsonValue] = {}
        revisions: dict[str, str] = {}
        for index, model in enumerate(saved.models):
            if model.subject != ticker:
                continue
            payload = (
                _project_market_context(
                    model.payload, full_result_ref=f"/inputs/generation/models/{index}/payload"
                )
                if model.model_id == "market_context"
                else deepcopy(model.payload)
            )
            if scope == "stock" and model.model_id == "company_growth":
                payload = project_company_growth(
                    payload, ticker=ticker, periods=company_growth_periods
                )
                payload["full_result_ref"] = f"/inputs/generation/models/{index}/payload"
            models[model.model_id] = payload
            revisions[model.model_id] = model.calculation_revision
        expected = (
            _LEGACY_COMPANY_MODELS
            if plan is None
            else (() if definition is None else definition.model_ids)
        )
        row: dict[str, object] = {
            "calculation_revisions": revisions,
            "not_generated": [name for name in expected if name not in models],
        }
        if plan is None:
            securities[ticker] = {**models, **row}
        else:
            row["generation_scope_status"] = (
                "outside_generation_scope" if definition is None else "planned"
            )
            row["models"] = models
            row["related_subjects"] = (
                [] if definition is None else list(definition.related_subjects)
            )
            if scope == "stock":
                securities[ticker] = row
            else:
                shared_subjects[ticker] = {"scope": scope, **row}
    if plan is not None:
        return cast(
            dict[str, JsonValue],
            {
                "analysis_source": saved.model_dump(mode="json", exclude={"models"}),
                "securities": securities,
                "shared_subjects": shared_subjects,
            },
        )
    common = saved.result("market_context", "market")
    return cast(
        dict[str, JsonValue],
        {
            "analysis_source": saved.model_dump(mode="json", exclude={"models"}),
            "market_context": None
            if common is None
            else _project_market_context(
                common.payload,
                full_result_ref=f"/inputs/generation/models/{saved.models.index(common)}/payload",
            ),
            "market_context_calculation_revision": None
            if common is None
            else common.calculation_revision,
            "securities": securities,
        },
    )
