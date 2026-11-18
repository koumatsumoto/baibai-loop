"""表示額の近似をL3の数値から切り離さず、未記録の精度は作らない。"""

from copy import deepcopy

import pytest
from tests.engine.machine_analysis.observations.financials.test_pdf_selection import models

from baibai_engine.investment_judgment.workflow.company_growth_projection import (
    project_company_growth,
)

_OLD_CHANGE_COLUMNS = [
    "to_start",
    "to_end",
    "metric",
    "from_start",
    "from_end",
    "from_value",
    "difference",
    "rate",
    "margin_points",
    "sign_change",
    "reason",
]


def test_pdf_generated_values_keep_their_own_precision_and_source_clocks():
    _, growth, _ = models()
    original = deepcopy(growth.payload)
    projected = project_company_growth(growth.payload, ticker="9268")
    latest = growth.payload["periods"][-1]
    for observation in latest["observations"]:
        if observation["measurement"] not in {"quarter", "annual_ttm"}:
            continue
        view = projected["latest_observations"][observation["measurement"]]
        row = view["row"]
        assert row["metric_precision"] == {
            metric: value["precision"]
            for metric, value in observation["metrics"].items()
            if "precision" in value
        }
        assert row["values"] == [value["value"] for value in observation["metrics"].values()]
        for metric, value in observation["metrics"].items():
            refs = row.get("metric_sources", {}).get(metric, row["sources"])
            assert [projected["documents"][ref] for ref in refs] == value["documents"]
    reported = projected["reported"]["rows"][-1]
    assert reported["metric_precision"]["parent_profit"] == "published_display_amounts"
    assert reported["metric_precision"]["operating_margin"] == (
        "derived_from_truncated_display_amounts"
    )
    assert growth.payload == original


def test_reported_changes_keep_precision_and_exact_comparative_source():
    _, growth, _ = models()
    projected = project_company_growth(growth.payload, ticker="9268", periods=1)
    table = projected["reported_yoy"]
    assert table["columns"] == [*_OLD_CHANGE_COLUMNS, "precision", "source_sha256"]
    for row in table["rows"]:
        fields = dict(zip(table["columns"], row, strict=True))
        change = next(
            change
            for change in growth.payload["changes"]
            if change["measurement"] == "reported"
            and all(fields[name] == change[name] for name in ("to_start", "to_end", "metric"))
        )
        assert all(fields[name] == change.get(name) for name in table["columns"])
    assert projected["latest_observations"]["quarter"]["yoy"] == [
        change
        for change in growth.payload["changes"]
        if change["measurement"] == "quarter"
        and change["to_end"] == growth.payload["periods"][-1]["period_end"]
    ]


@pytest.mark.parametrize(
    "precision",
    [
        "published_display_amounts",
        "derived_from_truncated_display_amounts",
        "derived_from_display_amounts_rounding_unspecified",
    ],
)
def test_recorded_precision_does_not_fill_missing_value_or_other_metric(precision):
    _, growth, _ = models(pdfs=[])
    payload = deepcopy(growth.payload)
    observation = next(
        value
        for value in payload["periods"][-1]["observations"]
        if value["measurement"] == "reported"
    )
    observation["metrics"]["operating_cash_flow"] = {
        "value": None,
        "reason": "operand_unavailable",
        "documents": [],
        "precision": precision,
    }
    projected = project_company_growth(payload, ticker="9268")
    row = projected["reported_cash_flow"]["rows"][-1]
    assert row["values"] == [None]
    assert row["missing"] == {"operating_cash_flow": "operand_unavailable"}
    assert row["metric_precision"] == {"operating_cash_flow": precision}
    assert "metric_precision" not in projected["reported"]["rows"][-1]


def test_unrecorded_legacy_precision_keeps_old_table_shape():
    _, growth, _ = models(pdfs=[])
    original = deepcopy(growth.payload)
    projected = project_company_growth(growth.payload, ticker="9268")
    rows = [
        *projected["reported"]["rows"],
        *projected["reported_cash_flow"]["rows"],
        *(view["row"] for view in projected["latest_observations"].values()),
    ]
    assert all("metric_precision" not in row for row in rows)
    assert projected["reported_yoy"]["columns"] == _OLD_CHANGE_COLUMNS
    assert all(len(row) == len(_OLD_CHANGE_COLUMNS) for row in projected["reported_yoy"]["rows"])
    assert growth.payload == original


def test_absent_saved_metric_is_not_a_disclosure_reason_or_another_columns_source():
    _, growth, _ = models(pdfs=[])
    raw = deepcopy(growth.payload)
    latest = next(o for o in raw["periods"][-1]["observations"] if o["measurement"] == "reported")
    docs = latest["metrics"]["sales"]["documents"]
    assert docs
    latest["metrics"] = {
        "sales": {"value": 0, "documents": docs},
        "operating_profit": {"value": -12, "documents": docs},
        "parent_profit": {"value": None, "reason": "operand_unavailable", "documents": docs},
        "operating_margin": {"value": None, "documents": docs},
    }
    del raw["metric_units"]["reported_basic_eps"]
    original = deepcopy(raw)
    view = project_company_growth(raw, ticker=growth.subject, periods=1)
    table = view["reported"]
    (row,) = table["rows"]
    assert list(zip(table["columns"], row["values"], strict=True)) == [
        ("sales", 0),
        ("operating_profit", -12),
        ("parent_profit", None),
        ("operating_margin", None),
        ("reported_basic_eps", None),
    ]
    assert row["unrecorded_metrics"] == ["reported_basic_eps"]
    assert row["missing"] == {"parent_profit": "operand_unavailable"}
    assert row["metric_sources"]["reported_basic_eps"] == []
    for name in latest["metrics"]:
        assert [view["documents"][ref] for ref in row["metric_sources"][name]] == docs
    assert table["units"]["reported_basic_eps"] is None
    assert table["units"]["operating_profit"] == original["metric_units"]["operating_profit"]
    (cash,) = view["reported_cash_flow"]["rows"]
    assert cash["unrecorded_metrics"] == ["operating_cash_flow"]
    assert cash["values"] == [None]
    assert cash["sources"] == []
    assert "missing" not in cash
    assert (row["period_start"], row["period_end"]) == (
        latest["period_start"],
        latest["period_end"],
    )
    assert raw == original
