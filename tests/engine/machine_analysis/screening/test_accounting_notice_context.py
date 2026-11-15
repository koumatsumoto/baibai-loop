"""会計変更の明示注意を、成長合致や比較可能性の認定と分けて初回へ残す。"""

from copy import deepcopy
from dataclasses import replace

import pytest
from pydantic import TypeAdapter
from tests.engine.machine_analysis.screening.test_economic_path_inputs import (
    metadata,
    models,
    payloads,
)
from tests.engine.machine_analysis.screening.test_economic_path_inputs import (
    project as economic_project,
)
from tests.engine.machine_analysis.screening.test_technical_financial import row

from baibai_engine.investment_judgment.workflow.screening_brief import (
    technical_financial_screening_brief,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
    discovery_display_order,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial_selection import (
    evaluate_technical_financial_rows,
    project_technical_financial_row,
)

NOTICE = {
    "disclosure_number": "synthetic-accounting-change",
    "disclosed_at": "2026-01-05",
    "disclosed_time": None,
    "period_start": "2025-01-01",
    "period_end": "2025-12-31",
    "document_payload_sha256": "b" * 64,
    "flags": {
        "ChgByASRev": "",
        "ChgNoASRev": "true",
        "ChgAcEst": None,
        "RetroRst": "false",
    },
}


def project(data):
    return project_technical_financial_row("1234", models=models(data), metadata=metadata())


def card(actual):
    saved = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(), "T", "G", (actual,), ("1234",), {}
    )
    return technical_financial_screening_brief(saved, lane=actual.screening_lane)["cards"][0]


@pytest.mark.parametrize("flag", ["ChgByASRev", "ChgNoASRev", "ChgAcEst", "RetroRst"])
def test_known_notice_reaches_card_without_disabling_or_clearing_hypotheses(flag):
    data = payloads()
    baseline = project(data)
    notice = deepcopy(NOTICE)
    notice["flags"] = {
        key: "true" if key == flag else value for key, value in notice["flags"].items()
    }
    data["company_financials"]["earnings_price"]["accounting_disclosures"] = [notice]
    before = deepcopy(data)
    expected = economic_project(data).context["accounting_change_context"]
    actual = project(data)
    assert actual.financial_paths["accounting_change_context"] == expected
    assert expected["disclosures"] == [notice]
    assert {
        k: v for k, v in actual.financial_paths.items() if k != "accounting_change_context"
    } == ({k: v for k, v in baseline.financial_paths.items() if k != "accounting_change_context"})
    assert actual.financial_gate == baseline.financial_gate
    assert actual.technical == baseline.technical
    assert actual.screening_lane == baseline.screening_lane
    assert discovery_display_order((actual,)) == discovery_display_order((baseline,))
    first = card(actual)
    assert first["financial_paths"]["accounting_change_context"] == expected
    first["financial_paths"]["accounting_change_context"]["disclosures"][0]["flags"].clear()
    assert actual.financial_paths["accounting_change_context"] == expected
    assert data == before


@pytest.mark.parametrize(
    "flags", [{}, {"RetroRst": "false"}, {"ChgNoASRev": ""}, {"ChgAcEst": None}]
)
def test_no_positive_notice_is_not_comparability_clearance(flags):
    data = payloads()
    data["company_financials"]["earnings_price"]["accounting_disclosures"] = [
        {**deepcopy(NOTICE), "flags": flags}
    ]
    context = card(project(data))["financial_paths"]["accounting_change_context"]
    assert context == {
        "read_semantics": "positive_native_flags_not_comparability_clearance",
        "disclosures": [],
    }


@pytest.mark.parametrize("mode", ["missing_field", "missing_earnings", "missing_model"])
def test_unrecorded_or_ungenerated_notice_is_not_completed_as_empty(mode):
    data = payloads()
    if mode == "missing_field":
        data["company_financials"]["earnings_price"].pop("accounting_disclosures")
    elif mode == "missing_earnings":
        data["company_financials"].pop("earnings_price")
    else:
        data.pop("company_financials")
    assert "accounting_change_context" not in project(data).financial_paths


@pytest.mark.parametrize(
    "lane", ["growth_research", "input_review", "alternative_financial_research"]
)
def test_notice_is_not_limited_to_growth_matches_or_financial_eligibility(lane):
    original = row("1234")
    context = {
        "read_semantics": "positive_native_flags_not_comparability_clearance",
        "disclosures": [deepcopy(NOTICE)],
    }
    actual = replace(
        original,
        screening_lane=lane,
        financial_paths={**original.financial_paths, "accounting_change_context": context},
    )
    assert card(actual)["financial_paths"]["accounting_change_context"] == context
    assert actual.financial_paths["matched_routes"] == []


@pytest.mark.parametrize("version", range(1, 11))
def test_legacy_policy_bytes_and_absence_are_not_backfilled_or_regenerated(version):
    old = TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method=f"technical-financial-discovery-v{version}"),
        "T",
        "G",
        (row("1234"),),
        ("1234",),
        {},
    )
    codec = TypeAdapter(TechnicalFinancialSelection)
    raw = codec.dump_json(old)
    restored = codec.validate_json(raw)
    assert codec.dump_json(restored) == raw
    brief = technical_financial_screening_brief(restored, comparison_tickers=("1234",))
    assert "accounting_change_context" not in brief["cards"][0]["financial_paths"]
    assert "accounting_change_context" not in brief["comparison_rows"][0]["financial_paths"]
    with pytest.raises(ValueError, match=r"historical.*read-only"):
        evaluate_technical_financial_rows(metadata(), tickers=(), rows={}, policy=restored.policy)
