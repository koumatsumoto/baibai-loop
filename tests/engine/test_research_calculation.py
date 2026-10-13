"""Draft arithmetic shares existing checks and leaves publication and inputs intact."""

import json
import socket
import sqlite3
from copy import deepcopy
from datetime import datetime

import pytest
from tests.helpers.research_v4 import pair_payload

from baibai_engine.research import workspace_cli
from baibai_engine.research.calculation import CalculationInput, calculate
from baibai_engine.research.calculation_cli import main
from baibai_engine.research.thesis import (
    ThesisDocument,
    ThesisReview,
    UnpublishedThesis,
    evaluate_thesis,
    thesis_core_hash,
)

NOW = datetime.fromisoformat("2026-09-07T18:00:00+09:00")


@pytest.fixture(autouse=True)
def forbid_database_and_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("calculate must not connect to a database or network")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)


def inputs_payload(*, numerator=1, denominator=3, numerator_unit="JPY", denominator_unit="JPY"):
    thesis, _ = pair_payload()
    payload = {key: deepcopy(thesis[key]) for key in ("input_snapshot", "valuation")}
    for name, value, unit in (
        ("numerator", numerator, numerator_unit),
        ("denominator", denominator, denominator_unit),
    ):
        payload["input_snapshot"]["facts"].append(
            dict(
                fact_id=name,
                fact_kind="other",
                value=value,
                unit=unit,
                as_of="2026-09-04",
                source_ids=["ir"],
            )
        )
    payload["derived"] = {
        "metrics": [
            dict(
                metric_id="ratio",
                unit="JPY_per_share"
                if denominator_unit == "shares" and numerator_unit == "JPY"
                else "ratio",
                as_of="2026-09-04",
                input_fact_ids=["numerator", "denominator"],
                assumption="同じ期間・株式basisの一次資料から独立確認",
                source_ids=["ir"],
            )
        ]
    }
    return payload


def evaluated_pair(thesis, review):
    document = ThesisDocument.model_validate(thesis)
    review = deepcopy(review)
    review["reviewed_thesis_sha256"] = thesis_core_hash(document)
    return evaluate_thesis(
        document,
        review=ThesisReview.model_validate(review),
        identity=UnpublishedThesis.DRAFT,
        now=NOW,
    )


def test_cli_generates_ratio_compatible_with_existing_tolerance_without_writes(tmp_path, capsys):
    payload = inputs_payload()
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    paths_before = set(tmp_path.rglob("*"))
    assert workspace_cli.main(["calculate", str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    metric = result["derived"]["metrics"][0]
    assert metric["value"] == 0.3333333333333333
    assert metric["formula"] == "ratio"
    assert metric["formula_version"] == "ratio-v1"
    thesis, review = pair_payload()
    thesis.update(
        input_snapshot=payload["input_snapshot"],
        derived=result["derived"],
        valuation=result["valuation"],
    )
    review["recalculated_projections"] = result["recalculated_projections"]
    assert evaluated_pair(thesis, review).decision_readiness == "ready"
    thesis["derived"]["metrics"][0]["value"] = 0.3333
    assert any("value mismatch" in error for error in evaluated_pair(thesis, review).errors)
    assert path.read_bytes() == before
    assert set(tmp_path.rglob("*")) == paths_before


@pytest.mark.parametrize(
    ("terminal", "cash", "months", "rate", "total", "annual", "pmax"),
    [
        (1204, 30, 12, 12, 23.4, 23.4, 1101.7857142857142),
        (1210, 0, 24, 10, 21, 10, 1000),
        (1210, 0, 6, 21, 21, 46.41, 1100),
        (0, 0, 12, 12, -100, -100, 0),
        (800, 200, 12, 12, 0, 0, 892.8571428571429),
    ],
)
def test_projection_numbers_use_recorded_price_horizon_rate(
    terminal, cash, months, rate, total, annual, pmax
):
    payload = inputs_payload()
    payload["valuation"].update(horizon_months=months, required_annual_return_pct=rate)
    payload["valuation"]["base"].update(
        terminal_value_per_share_yen=terminal, cash_distribution_per_share_yen=cash
    )
    result = calculate(CalculationInput.model_validate(payload))
    numbers = result["projection_numbers"]
    assert numbers["price_yen"] == 1000
    assert numbers["valuation_as_of"] == "2026-09-07"
    assert numbers["price_as_of"] == "2026-09-04"
    assert numbers["horizon_months"] == months
    assert numbers["required_annual_return_pct"] == rate
    assert numbers["base"]["total_value_yen"] == terminal + cash
    assert numbers["base"]["total_return_pct"] == pytest.approx(total)
    assert numbers["base"]["annualized_return_pct"] == pytest.approx(annual)
    assert numbers["base"]["pmax_raw_yen"] == pytest.approx(pmax)


@pytest.mark.parametrize(
    ("numerator_unit", "denominator_unit", "unit", "value"),
    [
        ("JPY", "shares", "JPY_per_share", 1204),
        ("JPY", "JPY", "ratio", 1204),
        ("shares", "shares", "ratio", 1204),
    ],
)
def test_existing_ratio_units(numerator_unit, denominator_unit, unit, value):
    result = calculate(
        CalculationInput.model_validate(
            inputs_payload(
                numerator=1204000000,
                denominator=1000000,
                numerator_unit=numerator_unit,
                denominator_unit=denominator_unit,
            )
        )
    )
    assert result["derived"]["metrics"][0]["unit"] == unit
    assert result["derived"]["metrics"][0]["value"] == value


@pytest.mark.parametrize("value", [True, "3", float("nan"), float("inf"), 10**31])
def test_invalid_ratio_fact_values(value):
    with pytest.raises(ValueError, match=r"numeric|validation error"):
        calculate(CalculationInput.model_validate(inputs_payload(numerator=value)))


@pytest.mark.parametrize(
    "fault",
    [
        "zero",
        "missing",
        "unit",
        "duplicate_fact",
        "duplicate_metric",
        "value",
        "formula",
        "ratio_overflow",
    ],
)
def test_invalid_ratio_definition_does_not_emit_partial_output(tmp_path, capsys, fault):
    payload = inputs_payload()
    metric = payload["derived"]["metrics"][0]
    if fault == "zero":
        payload["input_snapshot"]["facts"][-1]["value"] = 0
    elif fault == "missing":
        metric["input_fact_ids"][1] = "missing"
    elif fault == "unit":
        metric["unit"] = "percent"
    elif fault == "duplicate_fact":
        payload["input_snapshot"]["facts"].append(deepcopy(payload["input_snapshot"]["facts"][0]))
    elif fault == "duplicate_metric":
        payload["derived"]["metrics"].append(deepcopy(metric))
    elif fault == "ratio_overflow":
        payload["input_snapshot"]["facts"][-1]["value"] = 1e-310
    else:
        metric[fault] = 1 if fault == "value" else "eval"
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(payload))
    assert main([str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")


@pytest.mark.parametrize(
    "fault", ["missing", "unit", "adjusted", "zero", "negative", "bool", "string", "non_price"]
)
def test_invalid_projection_price(fault):
    payload = inputs_payload()
    price = payload["input_snapshot"]["facts"][0]
    if fault == "missing":
        payload["valuation"]["market_price_fact_id"] = "missing"
    elif fault == "unit":
        price["unit"] = "JPY"
    elif fault == "adjusted":
        price["price_basis"] = "last_close_adjusted"
    elif fault == "non_price":
        payload["valuation"]["market_price_fact_id"] = "numerator"
    else:
        price["value"] = {"zero": 0, "negative": -1, "bool": True, "string": "1000"}[fault]
    with pytest.raises(ValueError, match=r"price|positive"):
        calculate(CalculationInput.model_validate(payload))


@pytest.mark.parametrize("unresolved", [False, True])
def test_missing_valuation_has_no_manufactured_projection(unresolved):
    payload = inputs_payload()
    payload["input_snapshot"]["facts"] = payload["input_snapshot"]["facts"][1:]
    payload["valuation"] = (
        dict(
            status="unresolved",
            market_price_fact_id=None,
            horizon_months=None,
            required_annual_return_pct=None,
            base=None,
            downside=None,
            unresolved_reason="期間・価値未確定",
        )
        if unresolved
        else None
    )
    result = calculate(CalculationInput.model_validate(payload))
    assert result["projection_numbers"] is None
    assert result["recalculated_projections"] == []
    assert result["derived"]["metrics"][0]["value"] == 0.3333333333333333


def test_calculation_keeps_saved_core_and_independent_review_semantics():
    thesis, review = pair_payload()
    before = deepcopy((thesis, review))
    original_hash = thesis_core_hash(ThesisDocument.model_validate(thesis))
    author_inputs = {key: deepcopy(thesis[key]) for key in ("input_snapshot", "valuation")}
    author = calculate(CalculationInput.model_validate(author_inputs))
    # Equivalent numeric serialization must retain the existing canonical core/hash.
    thesis["valuation"] = author["valuation"]
    review["recalculated_projections"] = author["recalculated_projections"]
    assert thesis_core_hash(ThesisDocument.model_validate(thesis)) == original_hash
    assert evaluated_pair(thesis, review).decision_readiness == "ready"
    reviewer_inputs = deepcopy(author_inputs)
    reviewer_inputs["valuation"]["base"]["terminal_value_per_share_yen"] = 1260
    reviewer = calculate(CalculationInput.model_validate(reviewer_inputs))
    review["recalculated_projections"] = reviewer["recalculated_projections"]
    assert any("terminal" in error for error in evaluated_pair(thesis, review).errors)
    assert author_inputs == {key: before[0][key] for key in ("input_snapshot", "valuation")}
    # No prose parser: the same incorrect terminal from both passes still agrees.
    thesis["valuation"] = reviewer["valuation"]
    assert evaluated_pair(thesis, review).decision_readiness == "ready"


def test_legacy_ratio_reports_unit_and_value_errors_together():
    payload = inputs_payload()
    result = calculate(CalculationInput.model_validate(payload))
    thesis, review = pair_payload()
    thesis.update(input_snapshot=payload["input_snapshot"], derived=result["derived"])
    thesis["derived"]["metrics"][0].update(unit="percent", value=0.3333)
    errors = evaluated_pair(thesis, review).errors
    assert any("unit mismatch" in error for error in errors)
    assert any("value mismatch" in error for error in errors)
