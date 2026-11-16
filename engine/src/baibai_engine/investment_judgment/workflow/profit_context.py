"""固定された年利益の観測差を併読する。訂正の採択・候補再判定はしない。"""

from __future__ import annotations

from contextlib import suppress
from copy import deepcopy
from datetime import date
from math import isfinite

from pydantic import JsonValue, ValidationError

from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelection,
    GeneratedResearchSelectionV4,
    ResearchSelectionRecord,
)
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.models.company_financials.earnings import read_earnings_price
from baibai_engine.machine_analysis.models.company_growth.reading import CompanyGrowthReader


def _object(value: JsonValue | None) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _pick(source: dict[str, JsonValue], *names: str) -> dict[str, JsonValue]:
    return {name: deepcopy(source[name]) for name in names if name in source}


def _financial(model: GeneratedModel) -> dict[str, JsonValue]:
    source = _object(model.payload.get("earnings_price"))
    observation = _pick(
        source,
        "profit_ttm_yen",
        "statement_basis",
        "accounting_standard",
        "profit_components",
        "unavailable_reason",
    )
    # A partial old contract is evidence, not permission to infer its interval.
    period: tuple[date | None, date | None] = (None, None)
    with suppress(ValidationError):
        period = read_earnings_price(source).profit_period
    return {
        **_pick(model.payload, "as_of", "read_semantics", "earnings_basis"),
        "status": "recorded" if source else "not_recorded",
        "unit": "JPY",
        "period": [None if day is None else day.isoformat() for day in period],
        "observation": observation,
    }


def _growth(model: GeneratedModel) -> dict[str, JsonValue]:
    result = _pick(model.payload, "read_semantics", "comparison_basis")
    result["unit"] = _object(model.payload.get("metric_units")).get("parent_profit")
    try:
        reader = CompanyGrowthReader(model.payload)
    except ValidationError:
        return {**result, "status": "unreadable_saved_contract"}
    annual = reader.observation(reader.latest_period, "annual_ttm")
    if annual is None:
        return {**result, "status": "not_recorded"}
    raw = reader.saved_observation(annual)
    observation = _pick(raw, "statement_basis", "accounting_standard", "period_status")
    parent = _object(raw.get("metrics"))
    if "parent_profit" in parent:
        observation["parent_profit"] = deepcopy(parent["parent_profit"])
    return {
        **result,
        "status": "recorded",
        "period": [annual.period_start.isoformat(), annual.period_end.isoformat()],
        "observation": observation,
    }


def _number(value: JsonValue | None) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Integers are finite; converting a saved amount to float can overflow or lose precision.
    return None if isinstance(value, float) and not isfinite(value) else value


def _comparison(
    financial: dict[str, JsonValue], growth: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    cf, cg = _object(financial.get("observation")), _object(growth.get("observation"))
    left = _number(cf.get("profit_ttm_yen"))
    right = _number(_object(cg.get("parent_profit")).get("value"))
    reasons: list[JsonValue] = []
    if left is None or right is None or _object(cg.get("parent_profit")).get("reason"):
        reasons.append("profit_unavailable")
    periods = (financial.get("period"), growth.get("period"))
    if any(
        not isinstance(period, list) or len(period) != 2 or None in period for period in periods
    ):
        reasons.append("period_unavailable")
    elif periods[0] != periods[1]:
        reasons.append("period_mismatch")
    if cg.get("period_status") not in (None, "available"):
        reasons.append("growth_period_unavailable")
    for field, allowed in (
        ("statement_basis", {"consolidated", "nonconsolidated"}),
        ("accounting_standard", {"JP", "US", "IFRS", "JMIS"}),
    ):
        values = cf.get(field), cg.get(field)
        if any(not isinstance(value, str) or value not in allowed for value in values):
            reasons.append(f"{field}_unavailable")
        elif values[0] != values[1]:
            reasons.append(f"{field}_mismatch")
    units = financial.get("unit"), growth.get("unit")
    if any(not isinstance(unit, str) or not unit for unit in units):
        reasons.append("unit_unavailable")
    elif units[0] != units[1]:
        reasons.append("unit_mismatch")
    return {
        "state": "not_comparable"
        if reasons
        else "same_recorded_number"
        if left == right
        else "different_recorded_numbers",
        "reasons": reasons,
    }


def _row(
    models: dict[tuple[str, str], tuple[int, GeneratedModel]], ticker: str
) -> dict[str, JsonValue]:
    values: dict[str, JsonValue] = {"ticker": ticker}
    for name, reader in (("company_financials", _financial), ("company_growth", _growth)):
        found = models.get((name, ticker))
        if found is None:
            values[name] = {"status": "not_generated"}
            continue
        index, model = found
        values[name] = {
            **reader(model),
            "calculation_revision": model.calculation_revision,
            "full_result_ref": f"/inputs/generation/models/{index}/payload",
        }
    values["comparison"] = _comparison(
        _object(values["company_financials"]), _object(values["company_growth"])
    )
    return values


def saved_profit_context(
    selection: ResearchSelectionRecord, *, selection_hash: str
) -> dict[str, JsonValue] | None:
    """選定時のCFとGrowthの同期間観測を並べ、同数値を独立確認にしない。"""
    if not isinstance(selection, (GeneratedResearchSelection, GeneratedResearchSelectionV4)):
        return None
    saved = selection.inputs.generation
    models = {
        (model.model_id, model.subject): (index, model)
        for index, model in enumerate(saved.models)
        if model.model_id in {"company_financials", "company_growth"}
    }
    return {
        "role": "saved_annual_profit_observations_not_current_valuation",
        "selection_sha256": selection_hash,
        "generation_id": saved.generation_id,
        "generation_as_of": saved.as_of.isoformat(),
        "evaluation_time": saved.evaluation_time.isoformat(),
        "input_known_at": selection.input_known_at.isoformat(),
        "rows": [_row(models, target.ticker) for target in selection.targets],
    }
