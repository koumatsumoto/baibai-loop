"""現行の閲覧候補と経済的見送りを混ぜず、保存した一回から診断する。"""

import socket
from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from tests.engine.machine_analysis.screening.test_technical_financial import row
from tests.helpers.assessment_v1 import NOW
from tests.helpers.operation_proposal import PUBLISHED, proposal_for, seed_proposal
from tests.tools.test_inspect_decision_cohort import _assessment, _finish, _start, _unfinished
from tools.experiments.inspect_decision_cohort import inspect_decision_cohort

from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
    TechnicalFinancialSelection,
)


def saved_result(version=11):
    rows = (
        row("1234", routes=()),
        row("5678", state="input_unresolved"),
        replace(
            row("9012", state="not_selected"),
            financial_paths={"matched_routes": ["business_expansion"], "paths": []},
            screening_lane="alternative_financial_research",
        ),
        row("3456", state="not_selected"),
    )
    rows[0].financial_gate["matched_routes"] = ["profit_growth"]
    rows[1].financial_gate["checks"]["quarter_profit"] = {"value": -10, "state": "not_met"}
    rows[3].technical["observations"] = {"absolute_return": {"value": None}}
    return TechnicalFinancialSelection(
        TechnicalFinancialPolicy(method=f"technical-financial-discovery-v{version}", page_size=1),
        "saved-T",
        "saved-G",
        rows,
        ("9012", "1234", "5678"),
        {"planned_stocks": 4, "screening_lanes": {"input_review": 1}},
    )


@pytest.mark.parametrize("version", [5, 11])
@pytest.mark.parametrize("action", ["buy", "no_action"])
def test_saved_technical_cohort_keeps_all_lanes_and_research_outcomes(
    tmp_path, monkeypatch, version, action
):
    result = saved_result(version)
    original = deepcopy(result)
    db, runs, service, operation = _start(tmp_path, screening_result=result)
    reference = _assessment(db)
    service.record_assessment(operation.operation_id, reference, now=NOW)
    operation = _unfinished(service, operation, "5678")
    proposal = proposal_for(operation, actions={"1234": action})
    seed_proposal(db, proposal)
    service.record_proposal(operation.operation_id, proposal.proposal_id, now=PUBLISHED)
    completed = _finish(service, operation)
    runs.rename(tmp_path / "unavailable-runs")
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("network"))
    before = db.read_bytes()
    observed = inspect_decision_cohort(db_path=db, operation_id=completed.operation_id)
    baseline = observed["baseline"]
    assert [r["ticker"] for r in baseline["rows"]] == list(result.display_order)
    assert baseline["eligible_count"] is None
    assert baseline["screening_display_count"] == 3
    assert baseline["screening_summary"] == result.summary
    rows = {r["ticker"]: r for r in baseline["rows"]}
    for source in result.rows[:3]:
        assert rows[source.ticker]["candidate"] == asdict(source)
    assert rows["1234"]["proposal_item"]["action"] == action
    assert rows["5678"]["research_status"] == "unfinished"
    assert rows["5678"]["proposal_item"]["action"] == "defer"
    assert rows["9012"]["research_status"] == "not_selected"
    assert rows["9012"]["proposal_item"] is None
    assert baseline["screening_policy"]["method"] == result.policy.method
    assert db.read_bytes() == before
    assert result == original
    original_observed = deepcopy(observed)
    rows["5678"]["candidate"]["financial_gate"]["checks"].clear()
    assert (
        inspect_decision_cohort(db_path=db, operation_id=completed.operation_id)
        == original_observed
    )


def test_selected_outside_display_keeps_saved_diagnostic_without_promotion(tmp_path):
    result = saved_result()
    db, _runs, service, operation = _start(tmp_path, tickers=("3456",), screening_result=result)
    _unfinished(service, operation, "3456")
    completed = _finish(service, operation)
    baseline = inspect_decision_cohort(db_path=db, operation_id=completed.operation_id)["baseline"]
    outside = baseline["rows"][-1]
    assert outside["ticker"] == "3456"
    assert outside["candidate"] is None
    assert outside["screening_diagnostic"] == asdict(result.rows[-1])
    assert outside["screening_diagnostic"]["screening_lane"] == "no_current_hypothesis"
    assert outside["research_status"] == "unfinished"
    assert outside["proposal_item"] is None


def test_empty_research_keeps_display_set_not_all_universe_or_fake_rejection(tmp_path):
    result = saved_result()
    db, _runs, service, operation = _start(tmp_path, tickers=(), screening_result=result)
    completed = _finish(service, operation)
    baseline = inspect_decision_cohort(db_path=db, operation_id=completed.operation_id)["baseline"]
    assert [r["ticker"] for r in baseline["rows"]] == list(result.display_order)
    assert len(baseline["screening_universe_tickers"]) == 4
    assert all(r["research_status"] == "not_selected" for r in baseline["rows"])
    assert all(r["proposal_item"] is None for r in baseline["rows"])
    assert baseline["proposal_ref"] is None
