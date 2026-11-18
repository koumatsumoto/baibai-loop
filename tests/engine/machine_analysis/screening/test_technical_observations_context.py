"""市場の値が観測できても比較できない場合に、独立した財務仮説を消さない。"""

import pytest
from tests.engine.machine_analysis.screening.test_activity_context import no_other_match
from tests.engine.machine_analysis.screening.test_financial_growth import models, payloads
from tests.helpers.technical_discovery import _metadata

from baibai_engine.machine_analysis.screening.policies.discovery import project_discovery_row


@pytest.mark.parametrize("case", ["stale", "field_absent", "model_absent"])
def test_a_relative_price_gap_cannot_erase_a_financial_candidate(case):
    stock = no_other_match()
    # Sole positive technical route. Its source window must still match the stock return.
    stock[1].payload["price_path_relative"]["comparisons"]["R20"]["relative_return"] = 0.1
    financials, meta = models(payloads()), _metadata()
    before = project_discovery_row("1234", models=(*stock, *financials), metadata=meta)
    assert before["technical_state"] == "matched"
    path = stock[0].payload["price_path"]["observations"]
    if case == "stale":
        path["R20"]["window_end_date"] = "2026-11-12"
    elif case == "field_absent":
        del path["R20"]
    else:
        stock = stock[1:]
    after = project_discovery_row("1234", models=(*stock, *financials), metadata=meta)
    assert after["technical_state"] == "unavailable"
    assert after["price_ready"] is (case != "model_absent")
    assert after["screening_lane"] == before["screening_lane"] == "growth_research"
    assert after["matched_paths"] == before["matched_paths"]
