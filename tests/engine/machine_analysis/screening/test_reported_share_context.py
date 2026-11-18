"""報告EPSの株式基準未確認を利益成長・現在株数から補完しない。"""

from copy import deepcopy

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_financial_growth import payloads
from tests.engine.machine_analysis.screening.test_growth_period_context import project
from tests.engine.machine_analysis.screening.test_technical_financial import row

from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_screening_brief,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
)

FIELD = "reported_share_context"


def reported(data):
    return next(
        o
        for o in data["company_growth"]["periods"][-1]["observations"]
        if o["measurement"] == "reported"
    )


def comparison(data, *, value=10):
    change = deepcopy(data["company_growth"]["changes"][0])
    change.update(
        metric="reported_basic_eps",
        measurement="reported",
        from_start="2025-01-01",
        from_end="2025-09-30",
        to_start="2026-01-01",
        to_end="2026-09-30",
        from_value=8,
        to_value=value,
        difference=None,
        rate=None,
        reason="share_basis_unverified",
    )
    return change


def card(actual):
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(), "T", "G", (actual,), ("1234",), {}
    )
    return technical_financial_screening_brief(saved)["cards"][0]["financial_gate"][FIELD]


@pytest.mark.parametrize("value", [-10, 0, 10, None])
def test_basic_eps_and_reported_period_preserve_unknown_share_comparison(value):
    data = payloads()
    original_gate = project(data).financial_gate
    obs = reported(data)
    obs["metrics"].update(
        parent_profit={"value": 700, "reason": None, "documents": []},
        reported_basic_eps={
            "value": value,
            "reason": "not_observed" if value is None else None,
            "documents": [],
        },
        average_shares={"value": 60, "reason": None, "documents": []},
    )
    data["company_growth"]["metric_units"].update(
        parent_profit="JPY",
        reported_basic_eps="JPY_per_source_reported_share",
        average_shares="shares",
    )
    change = comparison(data, value=value)
    data["company_growth"]["changes"].append(change)
    data["company_financials"]["financial"].update(
        shares_outstanding=999, shares_ex_treasury=800, forecast_eps_yen=20
    )
    before = deepcopy(data)
    actual = project(data)
    context = card(actual)
    assert context["reported"]["measurement"] == "reported"
    assert context["reported"]["period_start"] == "2026-01-01"
    assert context["reported"]["metrics"] == {
        k: obs["metrics"][k] for k in ("parent_profit", "reported_basic_eps", "average_shares")
    }
    assert context["year_over_year_observations"]["reported_basic_eps"] == [change]
    assert context["metric_units"]["reported_basic_eps"] == "JPY_per_source_reported_share"
    assert actual.financial_gate["checks"] == original_gate["checks"]
    context["reported"]["metrics"]["average_shares"]["value"] = 999
    context["year_over_year_observations"]["reported_basic_eps"][0]["rate"] = 1
    assert (
        actual.financial_gate[FIELD]["year_over_year_observations"]["reported_basic_eps"][0]["rate"]
        is None
    )
    assert data == before


def test_competing_same_period_comparisons_are_not_selected_or_recomputed():
    data = payloads()
    changes = [comparison(data, value=10), comparison(data, value=20)]
    data["company_growth"]["changes"].extend(changes)
    actual = project(data)
    assert card(actual)["year_over_year_observations"]["reported_basic_eps"] == changes


def test_unrecorded_comparison_default_is_not_presented_as_a_saved_change():
    data = payloads()
    change = comparison(data)
    del change["comparison"]
    data["company_growth"]["changes"].append(change)
    assert card(project(data))["year_over_year_observations"]["reported_basic_eps"] == []


def test_missing_units_and_metric_are_not_filled_from_financial_snapshot():
    data = payloads()
    data["company_growth"].pop("metric_units", None)
    data["company_financials"]["financial"].update(eps=10, shares_outstanding=999)
    context = card(project(data))
    assert context["metric_units"] == {}
    assert "reported_basic_eps" not in context["reported"]["metrics"]
    del data["company_growth"]
    assert project(data).financial_gate[FIELD]["reported"] is None


def test_old_v19_does_not_backfill_reported_share_context():
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method="technical-financial-discovery-v19"),
        "T",
        "G",
        (row("1234"),),
        ("1234",),
        {},
    )
    codec = TypeAdapter(TechnicalFinancialSelection)
    original = codec.dump_json(saved)
    restored = codec.validate_json(original)
    assert codec.dump_json(restored) == original
    assert FIELD not in technical_financial_screening_brief(restored)["cards"][0]["financial_gate"]
