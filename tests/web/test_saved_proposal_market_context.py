"""保存時の市場反証は現在の市場評価でも個社価値の採択でもない。"""

from contextlib import closing
from datetime import timedelta

import pytest
from tests.engine.investment_judgment.judgment_retrospective.test_retrospective_market_context import (
    _case,
)
from tests.engine.investment_judgment.trade_proposal.test_saved_market_context import (
    _market_document,
)
from tests.helpers.proposal import PUBLISHED

from baibai_engine.investment_judgment.market_assessment.service import AssessmentService
from baibai_engine.investment_judgment.trade_proposal.query import load_investment_proposal
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import saved_market_assessment
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_web.sources.proposal import read_proposal_views


@pytest.mark.parametrize("action", ["no_action", "defer"])
def test_saved_final_comparison_retains_market_countercase_and_original_clocks(tmp_path, action):
    case, proposal, runs, original = _case(tmp_path, action=action, unfinished=action == "defer")
    expected = saved_market_assessment(selection_binding(case.operation.payload))
    operations = OperationService(case.app)
    operations.complete(
        case.operation.operation_id,
        proposal_id=proposal.proposal_id,
        result="合成保存",
        next="後日比較",
        completed_at=PUBLISHED + timedelta(minutes=1),
    )
    AssessmentService(case.app).publish(
        _market_document(suffix="later", published_at=PUBLISHED + timedelta(minutes=2)),
        expected_head=original.context_id,
    )
    runs.rename(tmp_path / "unused-runs.sqlite")
    case.market.rename(tmp_path / "unused-market.sqlite")
    before = case.app.read_bytes()
    latest, history = read_proposal_views(case.app)
    view = latest.proposal
    assert view is not None
    market = view.model_dump(mode="json").get("saved_market_context")
    assert market == {
        "source": "proposal_fixed_selection",
        "selection_sha256": proposal.research_scope.selection_sha256,
        "status": "retained",
        "assessment": expected,
        "reason": None,
    }
    assert market["assessment"]["known_at"] != market["assessment"]["published_at"]
    assert view.comparison == proposal.comparison
    assert view.items[0].action == action
    if action == "defer":
        assert view.items[0].assessment is None
    assert history.proposals == [view]
    with closing(connect_read_only(case.app)) as connection:
        assert load_investment_proposal(connection, proposal.proposal_id) == proposal
    assert case.app.read_bytes() == before


def test_not_recorded_at_selection_is_not_replaced_by_later_market_head(tmp_path):
    case, proposal, _runs, _ = _case(tmp_path, market=False)
    AssessmentService(case.app).publish(_market_document(), expected_head=None)
    before = case.app.read_bytes()
    view = read_proposal_views(case.app).latest.proposal
    assert view.saved_market_context.status == "not_recorded"
    assert view.saved_market_context.assessment is None
    assert view.items[0].assessment.assessment_id == proposal.items[0].assessment_ref.assessment_id
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("failure", ["operation", "unreadable", "hash_mismatch"])
def test_optional_fixed_market_failure_does_not_erase_the_saved_judgment(tmp_path, failure):
    import json
    from copy import deepcopy

    from baibai_engine.storage.sqlite.application.write import connect_rw

    case, proposal, _runs, _ = _case(tmp_path)
    with closing(connect_rw(case.app)) as connection, connection:
        if failure == "operation":
            connection.execute(
                "DELETE FROM operation_session WHERE operation_id=?", (case.operation.operation_id,)
            )
        else:
            payload = deepcopy(case.operation.payload.model_dump(mode="json"))
            if failure == "unreadable":
                payload["artifacts"] = "unreadable fixture"
            else:
                for artifact in payload["artifacts"]:
                    if artifact.get("kind") == "research_selection":
                        artifact["selection_summary"] = "別の選定へ変更した合成対照"
            connection.execute(
                "UPDATE operation_session SET payload=? WHERE operation_id=?",
                (json.dumps(payload), case.operation.operation_id),
            )
    before = case.app.read_bytes()
    view = read_proposal_views(case.app).latest.proposal
    assert view.items[0].assessment.estimated_model_chart is None
    assert view.saved_market_context.status == "unavailable"
    assert (
        view.saved_market_context.reason
        == {
            "operation": "operation_not_found",
            "unreadable": "selection_unreadable",
            "hash_mismatch": "selection_hash_mismatch",
        }[failure]
    )
    assert view.saved_market_context.assessment is None
    assert view.items[0].action == proposal.items[0].action
    assert view.comparison == proposal.comparison
    assert case.app.read_bytes() == before


def test_old_market_clock_is_not_backfilled_and_returned_value_is_detached(tmp_path, monkeypatch):
    from baibai_engine.investment_judgment.market_assessment.projection import assessment_projection

    def legacy_projection(record, *, as_of):
        result = assessment_projection(record, as_of=as_of)
        result.pop("known_at", None)
        return result

    with monkeypatch.context() as legacy:
        for module in ("workflow.workspace.selected_prepare", "workflow.operation.validation"):
            legacy.setattr(
                f"baibai_engine.investment_judgment.{module}.assessment_projection",
                legacy_projection,
            )
        case, proposal, _runs, _ = _case(tmp_path)
    before = case.app.read_bytes()
    view = read_proposal_views(case.app).latest.proposal
    expected = saved_market_assessment(selection_binding(case.operation.payload))
    assert view.saved_market_context.assessment == expected
    assert "known_at" not in view.saved_market_context.assessment
    view.saved_market_context.assessment["counter_evidence"].clear()
    assert read_proposal_views(case.app).latest.proposal.saved_market_context.assessment == expected
    with closing(connect_read_only(case.app)) as connection:
        assert load_investment_proposal(connection, proposal.proposal_id) == proposal
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("old", ["absent", "null"])
def test_older_readmodel_payload_does_not_invent_market_evidence(tmp_path, old):
    from baibai_web.readmodel.proposal_models import SavedProposalView

    case, _proposal, _runs, _ = _case(tmp_path)
    payload = read_proposal_views(case.app).latest.proposal.model_dump(mode="json")
    if old == "absent":
        del payload["saved_market_context"]
    else:
        payload["saved_market_context"] = None
    assert SavedProposalView.model_validate(payload).saved_market_context is None


def test_market_reference_mismatch_never_substitutes_another_assessment(tmp_path):
    from baibai_engine.investment_judgment.trade_proposal.query import saved_proposal_market_context

    case, proposal, _runs, _ = _case(tmp_path)
    mismatch = proposal.model_copy(update={"market_context_assessment_id": "another-market"})
    with closing(connect_read_only(case.app)) as connection:
        result = saved_proposal_market_context(connection, mismatch)
    assert result["status"] == "unavailable"
    assert result["assessment"] is None
    assert result["reason"] == "market_context_reference_mismatch"


@pytest.mark.parametrize("duplicate", [False, True])
def test_only_fixed_artifact_identity_not_current_checkpoint_controls_market_read(
    tmp_path, duplicate
):
    import json
    from copy import deepcopy

    from baibai_engine.storage.sqlite.application.write import connect_rw

    case, _proposal, _runs, _ = _case(tmp_path)
    payload = deepcopy(case.operation.payload.model_dump(mode="json"))
    payload["checkpoint"] = None  # Unrelated current metadata is not historical evidence.
    if duplicate:
        payload["artifacts"].append(
            next(item for item in payload["artifacts"] if item.get("kind") == "research_selection")
        )
    with closing(connect_rw(case.app)) as connection, connection:
        connection.execute(
            "UPDATE operation_session SET payload=? WHERE operation_id=?",
            (json.dumps(payload), case.operation.operation_id),
        )
    before = case.app.read_bytes()
    view = read_proposal_views(case.app).latest.proposal
    if duplicate:
        assert view.saved_market_context.status == "unavailable"
        assert view.saved_market_context.reason == "selection_unreadable"
    else:
        assert view.saved_market_context.status == "retained"
        assert view.saved_market_context.assessment == saved_market_assessment(
            selection_binding(case.operation.payload)
        )
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("unfinished", [True, False])
def test_issuer_label_survives_without_live_master_and_does_not_create_an_assessment(
    tmp_path, monkeypatch, unfinished
):
    from functools import partial

    from tests.engine.investment_judgment.trade_proposal import test_saved_market_context as setup
    from tests.helpers.generated_selection import seed_generated_selection

    from baibai_engine.machine_analysis.generation.contracts import GeneratedModel

    fixed = GeneratedModel(
        model_id="market_context",
        subject="1234",
        calculation_revision="fixture",
        payload={
            "master": {
                "name": "当時の親会社",
                "sector_33": "合成業種",
                "snapshot_date": setup.START.date().isoformat(),
            }
        },
    )
    monkeypatch.setattr(
        setup, "seed_generated_selection", partial(seed_generated_selection, models=(fixed,))
    )
    case, _, runs, _ = _case(
        tmp_path, action="defer" if unfinished else "no_action", unfinished=unfinished
    )
    runs.rename(tmp_path / "unused-runs.sqlite")
    case.market.rename(tmp_path / "unused-market.sqlite")
    latest, history = read_proposal_views(case.app)
    assert latest.proposal is not None
    viewed = latest.proposal.items[0]
    assert history.proposals[0].items[0] == viewed
    if unfinished:
        assert (viewed.company_name, viewed.sector) == ("当時の親会社", "合成業種")
        assert viewed.unfinished is not None
        assert viewed.assessment is None
    else:
        signed = case.pair.document.input_snapshot
        assert signed.company_name != "当時の親会社"
        assert (viewed.company_name, viewed.sector) == (signed.company_name, signed.sector)
