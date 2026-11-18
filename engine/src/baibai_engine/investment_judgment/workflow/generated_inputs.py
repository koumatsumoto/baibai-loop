"""固定したモデル生成をL3へ渡す。再計算・旧ERへのfallback・候補再選定はしない。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import cast

from pydantic import JsonValue

from baibai_engine.investment_judgment.workflow.company_growth_projection import (
    project_company_growth,
)
from baibai_engine.machine_analysis.generation.contracts import ModelInputSlice
from baibai_engine.primitives.json import canonical_json

_LEGACY_COMPANY_MODELS = (
    "company_financials",
    "company_growth",
    "technical_analysis",
    "external_assessment",
    "market_context",
)
_PROPOSAL_MODELS = frozenset(
    {
        "external_assessment",
        "market_context",
        "industry_trends",
        "social_trends",
        "technical_analysis",
    }
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
    common_json = {key: canonical_json(value) for key, value in common.items()}
    for row, own in semantics:
        row["read_semantics"] = {
            key: value
            for key, value in own.items()
            if key not in common or canonical_json(value) != common_json[key]
        }
    reading["read_semantics_inheritance"] = (
        "Each series inherits reading.read_semantics; its read_semantics keys override."
    )
    payload["full_result_ref"] = full_result_ref
    return payload


def _analysis_source(saved: ModelInputSlice) -> dict[str, JsonValue]:
    """表示省略を入力欠測と混同しないよう、固定した完全入力への参照を残す。"""
    source = saved.model_dump(mode="json", exclude={"models", "inputs"})
    inputs = {}
    for name, identity in saved.inputs.items():
        entry = identity.model_dump(mode="json", exclude={"descriptor"})
        if identity.kind == "native_l1":
            descriptor = identity.descriptor
            entry["summary"] = deepcopy(
                descriptor.get("summary", {"status": "not_recorded"})
                if descriptor
                else {"status": "not_recorded"}
            )
            segment = name.replace("~", "~0").replace("/", "~1")
            entry["full_result_ref"] = f"/inputs/generation/inputs/{segment}/descriptor"
        inputs[name] = entry
    source["inputs"] = inputs
    return source


def _r20_index_sector_windows(market: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """共通暦R20と個社20barの同業欄は、同じ「20日」でも端点がずれる。"""

    def endpoints(*path: str) -> list[JsonValue] | None:
        value: JsonValue | None = market
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if not isinstance(value, dict):
            return None
        start, end = value.get("comparison_start_date"), value.get("comparison_end_date")
        if not isinstance(start, str) or not isinstance(end, str):
            return None
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError:
            return None
        return [first.isoformat(), last.isoformat()] if first < last else None

    index = endpoints("price_path_relative", "comparisons", "R20")
    sector = endpoints("relative", "sector")
    return {
        "index_r20_window": index,
        "sector_20bar_window": sector,
        "alignment": "endpoints_unavailable"
        if index is None or sector is None
        else "same_recorded_endpoints"
        if index == sector
        else "different_recorded_endpoints",
    }


def project_generated_inputs(
    saved: ModelInputSlice,
    *,
    company_growth_periods: int | None = 12,
    proposal_view: bool = False,
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
        available = {model.model_id for model in saved.models if model.subject == ticker}
        for index, model in enumerate(saved.models):
            if model.subject != ticker:
                continue
            if proposal_view and model.model_id not in _PROPOSAL_MODELS:
                continue
            payload: dict[str, JsonValue]
            if proposal_view and scope == "stock" and model.model_id == "technical_analysis":
                # 選定時の中期経路を短期の市場/同業比較と併読する。欠測や別期限を現在値で補わない。
                payload = {
                    key: deepcopy(model.payload[key])
                    for key in (
                        "as_of",
                        "history_end_date",
                        "price_basis",
                        "read_semantics",
                        "price_momentum_revision",
                        "price_momentum",
                    )
                    if key in model.payload
                }
                payload["full_result_ref"] = f"/inputs/generation/models/{index}/payload"
            elif scope == "stock" and model.model_id == "company_growth":
                payload = project_company_growth(
                    model.payload, ticker=ticker, periods=company_growth_periods
                )
                payload["full_result_ref"] = f"/inputs/generation/models/{index}/payload"
            elif model.model_id == "market_context":
                payload = _project_market_context(
                    model.payload, full_result_ref=f"/inputs/generation/models/{index}/payload"
                )
            else:
                payload = deepcopy(model.payload)
            models[model.model_id] = payload
            revisions[model.model_id] = model.calculation_revision
        expected = (
            _LEGACY_COMPANY_MODELS
            if plan is None
            else (() if definition is None else definition.model_ids)
        )
        row: dict[str, object] = {
            "calculation_revisions": revisions,
            "not_generated": [name for name in expected if name not in available],
        }
        if proposal_view:
            row["omitted_from_view"] = sorted(available - _PROPOSAL_MODELS)
            market = models.get("market_context")
            if scope == "stock" and isinstance(market, dict):
                row["r20_index_sector_windows"] = _r20_index_sector_windows(market)
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
                "analysis_source": _analysis_source(saved),
                "securities": securities,
                "shared_subjects": shared_subjects,
            },
        )
    common = saved.result("market_context", "market")
    return cast(
        dict[str, JsonValue],
        {
            "analysis_source": _analysis_source(saved),
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
