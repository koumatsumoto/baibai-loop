"""ID移行の原公開canonicalから市場断片だけを読み、運用値へ付け替えない。"""

import json
from contextlib import closing
from copy import deepcopy
from datetime import timedelta

import pytest
from tests.engine.investment_judgment.judgment_retrospective.test_retrospective_market_context import (
    _scaffold,
)
from tests.engine.investment_judgment.trade_proposal.test_saved_market_context import (
    _market_document,
)
from tests.engine.investment_judgment.workflow import test_selection_evidence as evidence_fixture
from tests.helpers.assessment_history import publish_at_declared_time
from tests.helpers.proposal import PUBLISHED, read_saved_proposal

from baibai_engine.investment_judgment.market_assessment.projection import assessment_projection
from baibai_engine.investment_judgment.market_assessment.query import assessment_by_id
from baibai_engine.investment_judgment.trade_proposal.query import saved_proposal_market_context
from baibai_engine.investment_judgment.workflow.selection_evidence import read_selection_evidence
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_web.sources.proposal import read_proposal_views


def _case(tmp_path, monkeypatch, *, market=True, legacy_clock=False, **kwargs):
    seed = evidence_fixture.seed_generated_selection

    def project(record, *, as_of):
        projected = assessment_projection(record, as_of=as_of)
        if legacy_clock:
            projected.pop("known_at", None)
        return projected

    def with_market(**seed_kwargs):
        selected = seed(**seed_kwargs)
        if not market:
            return selected
        document = _market_document()
        publish_at_declared_time(seed_kwargs["db"], document, expected_head=None)
        record = assessment_by_id(seed_kwargs["db"], context_id=document.context_id)
        return selected.model_copy(
            update={
                "inputs": selected.inputs.model_copy(
                    update={
                        "saved_market_context_assessment": project(
                            record, as_of=selected.research_as_of
                        )
                    }
                )
            }
        )

    monkeypatch.setattr(evidence_fixture, "seed_generated_selection", with_market)
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.workflow.operation.validation.assessment_projection",
        project,
    )
    return evidence_fixture.evidence_case(tmp_path, monkeypatch, **kwargs)


@pytest.mark.parametrize("generation_version", [1, 2])
@pytest.mark.parametrize("selection_version", [3, 4])
def test_published_market_fragment_is_retained_without_reinterpreting_old_models(
    tmp_path, monkeypatch, generation_version, selection_version
):
    # An exact operational receipt may contain different presentation values. The
    # publication's original fragment, not the operational one, is the evidence.
    parse = evidence_fixture.parse_selection

    def operational(raw):
        raw = deepcopy(raw)
        raw["inputs"]["saved_market_context_assessment"]["summary"] = "運用側の別表示"
        return parse(raw)

    monkeypatch.setattr(evidence_fixture, "parse_selection", operational)
    case, proposal, current, canonical = _case(
        tmp_path,
        monkeypatch,
        legacy_clock=True,
        generation_version=generation_version,
        selection_version=selection_version,
    )
    expected = json.loads(canonical)["inputs"]["saved_market_context_assessment"]
    assert current.inputs.saved_market_context_assessment != expected
    assert "known_at" not in expected
    publish_at_declared_time(
        case.app,
        _market_document(suffix="later", published_at=PUBLISHED + timedelta(days=1)),
        expected_head=expected["context_id"],
    )
    case.runs.rename(tmp_path / "unused-runs.sqlite")
    case.market.rename(tmp_path / "unused-market.sqlite")
    before = case.app.read_bytes()
    with closing(connect_read_only(case.app)) as connection:
        connection.execute("BEGIN")
        context = saved_proposal_market_context(connection, proposal)
    assert context == {
        "source": "proposal_fixed_selection",
        "selection_sha256": proposal.research_scope.selection_sha256,
        "status": "retained",
        "assessment": expected,
    }
    view = next(
        row
        for row in read_proposal_views(case.app).history.proposals
        if row.proposal_id == proposal.proposal_id
    )
    assert view.saved_market_context.assessment == expected
    assert view.comparison == proposal.comparison
    assert view.items[0].action == proposal.items[0].action
    assert _scaffold(case.app, proposal)["context"]["origin"]["market_context"] == context
    assert read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == proposal
    evidence = read_selection_evidence(
        operation_id=case.operation.operation_id,
        sha256=proposal.research_scope.selection_sha256,
        proposal_id=proposal.proposal_id,
        db_path=case.app,
    )
    assert evidence["canonical_json"] == canonical
    context["assessment"]["counter_evidence"].clear()
    with closing(connect_read_only(case.app)) as connection:
        assert saved_proposal_market_context(connection, proposal)["assessment"] == expected
    assert case.app.read_bytes() == before


@pytest.mark.parametrize(
    "damage", ["active", "operational_hash", "canonical", "duplicate_selection", "no_receipt"]
)
def test_damaged_receipt_never_substitutes_operational_or_current_market(
    tmp_path, monkeypatch, damage
):
    case, proposal, _, _ = _case(tmp_path, monkeypatch, damage=damage)
    before = case.app.read_bytes()
    with closing(connect_read_only(case.app)) as connection:
        result = saved_proposal_market_context(connection, proposal)
    assert result["status"] == "unavailable"
    assert result["assessment"] is None
    assert result["reason"] == (
        "selection_unreadable" if damage == "duplicate_selection" else "selection_hash_mismatch"
    )
    assert read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == proposal
    assert case.app.read_bytes() == before


def test_absent_original_market_is_not_filled_from_later_head(tmp_path, monkeypatch):
    case, proposal, _, _ = _case(tmp_path, monkeypatch, market=False)
    publish_at_declared_time(case.app, _market_document(), expected_head=None)
    before = case.app.read_bytes()
    with closing(connect_read_only(case.app)) as connection:
        result = saved_proposal_market_context(connection, proposal)
    assert result["status"] == "not_recorded"
    assert result["assessment"] is None
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("mismatch", ["operation_id", "selection_sha256", "market_reference"])
def test_original_receipt_still_requires_exact_proposal_scope_and_market_id(
    tmp_path, monkeypatch, mismatch
):
    case, proposal, _, _ = _case(tmp_path, monkeypatch)
    if mismatch == "market_reference":
        changed = proposal.model_copy(update={"market_context_assessment_id": "different-market"})
    else:
        changed = proposal.model_copy(
            update={
                "research_scope": proposal.research_scope.model_copy(update={mismatch: "f" * 64})
            }
        )
    before = case.app.read_bytes()
    with closing(connect_read_only(case.app)) as connection:
        result = saved_proposal_market_context(connection, changed)
    assert result["status"] == "unavailable"
    assert result["assessment"] is None
    assert case.app.read_bytes() == before
