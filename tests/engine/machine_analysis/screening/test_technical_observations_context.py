"""市場の値が観測できても比較できない場合に、独立した財務仮説を消さない。"""

import pytest
from tests.engine.machine_analysis.screening.test_financial_growth import models, payloads
from tests.helpers.technical_discovery import _metadata, _models

from baibai_engine.machine_analysis.screening.policies.discovery import project_discovery_row
from baibai_engine.machine_analysis.screening.policies.technical_discovery import (
    evaluate_technical_strategy,
)


@pytest.mark.parametrize(
    ("case", "value", "reason"),
    [
        ("stale", 0.1, "price_date_mismatch_or_unknown"),
        ("field_absent", None, "not_recorded"),
        ("model_absent", None, "model_not_generated"),
    ],
)
def test_relative_price_evidence_is_not_financial_eligibility(case, value, reason):
    stock = _models()
    financials, metadata = models(payloads()), _metadata()
    before = project_discovery_row("1234", models=(*stock, *financials), metadata=metadata)
    path = stock[0].payload["price_path"]["observations"]
    if case == "stale":
        path["R20"]["window_end_date"] = "2026-11-12"
    elif case == "field_absent":
        del path["R20"]
    else:
        stock = stock[1:]

    technical = evaluate_technical_strategy("1234", models=stock, metadata=metadata)
    observation = technical["observations"]["R20"]
    assert (observation["value"], observation["unavailable_reason"]) == (value, reason)
    if case == "stale":
        assert observation["window"]["window_end_date"] == "2026-11-12"
        assert technical["expected_price_date"] == "2026-11-13"
    assert "R20_positive" in technical["routes"]["trend_continuation"]["missing_conditions"]

    after = project_discovery_row("1234", models=(*stock, *financials), metadata=metadata)
    assert after["screening_lane"] == before["screening_lane"] == "growth_research"
    assert after["matched_paths"] == before["matched_paths"]
    assert after["technical_state"] == technical["state"]
