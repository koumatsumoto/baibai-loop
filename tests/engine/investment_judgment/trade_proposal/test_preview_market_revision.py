"""市場判断の新しい参照を、個社への経済影響や旧提案の差替えにしない。"""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from tests.engine.investment_judgment.trade_proposal.test_corrected_proposal_preview import _draft
from tests.engine.investment_judgment.trade_proposal.test_saved_market_context import (
    _market_document,
    _prepare,
)
from tests.helpers.assessment_v1 import assessment_id
from tests.helpers.proposal import PUBLISHED, backend_case, read_saved_proposal

from baibai_engine.investment_judgment.market_assessment.service import AssessmentService
from baibai_engine.investment_judgment.stock_assessment.store import load_reviewed_assessment
from baibai_engine.investment_judgment.trade_proposal.preview import preview_current_terms
from baibai_engine.investment_judgment.trade_proposal.query import saved_proposal_market_context
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.storage.sqlite.application.read import connect_read_only


def _publish(case, *, action="no_action"):
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    draft = _draft(case, action)
    return service, draft, service.publish(draft)


def _preview(case, saved, *, now=PUBLISHED + timedelta(minutes=3)):
    return preview_current_terms(
        proposal_id=saved.proposal_id, db_path=case.app, sqlite_path=case.market, now=now
    )


def _later_document():
    return _market_document(suffix="later").model_copy(
        update={
            "known_at": PUBLISHED + timedelta(minutes=1),
            "published_at": PUBLISHED + timedelta(minutes=2),
            "summary": "合成：借換条件の悪化。個社への伝達は未確認。",
        }
    )


def _fixed_case(tmp_path, document):
    app, market, runs, operation = _prepare(tmp_path, market_document=document)
    with closing(connect_read_only(app)) as connection:
        pair = load_reviewed_assessment(connection, assessment_id())
    return SimpleNamespace(app=app, market=market, runs=runs, operation=operation, pair=pair)


@pytest.mark.parametrize("action", ["buy", "hold", "sell", "no_action", "defer"])
def test_new_market_reference_is_nonblocking_and_never_changes_the_saved_economic_plan(
    tmp_path, action
):
    case = backend_case(tmp_path, holding=action in {"hold", "sell"}, broken=action == "sell")
    service, draft, saved = _publish(case, action=action)
    baseline = _preview(case, saved)
    assert saved.market_context_assessment_id is None
    later = _later_document()
    AssessmentService(case.app).publish(later, expected_head=None)
    before = case.app.read_bytes()

    preview = _preview(case, saved)
    assert preview.status == ("blocked" if baseline.status == "blocked" else "changed")
    reasons = [r for r in preview.change_reasons if r.code == "market_assessment_revision_changed"]
    assert len(reasons) == 1
    assert "保存参照=未記録" in reasons[0].message
    assert later.context_id in reasons[0].message
    assert later.as_of.isoformat() in reasons[0].message
    assert later.known_at.isoformat() in reasons[0].message
    assert later.published_at.isoformat() in reasons[0].message
    assert "個社への影響を認定" in reasons[0].message
    assert preview.items == baseline.items
    assert preview.current_inputs == baseline.current_inputs
    assert preview.warnings == baseline.warnings
    assert preview.portfolio_exposure == baseline.portfolio_exposure
    assert tuple(r for r in preview.change_reasons if r not in reasons) == baseline.change_reasons
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert service.publish(draft) == saved
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("same_claims", [False, True])
def test_fixed_selection_market_context_stays_exact_when_a_different_reference_is_available(
    tmp_path, same_claims
):
    original = _market_document()
    case = _fixed_case(tmp_path, original)
    service, draft, saved = _publish(case)
    with closing(connect_read_only(case.app)) as connection:
        fixed = saved_proposal_market_context(connection, saved)
    baseline = _preview(case, saved)
    assert baseline.status == "unchanged"
    later = _later_document()
    if same_claims:
        later = later.model_copy(update={"summary": original.summary})
    AssessmentService(case.app).publish(later, expected_head=original.context_id)
    case.runs.rename(tmp_path / "unused-runs.sqlite")
    before = case.app.read_bytes()

    preview = _preview(case, saved)
    assert preview.status == "changed"
    assert [r.code for r in preview.change_reasons] == ["market_assessment_revision_changed"]
    assert original.context_id in preview.change_reasons[0].message
    assert later.context_id in preview.change_reasons[0].message
    assert "経済変化" in preview.change_reasons[0].message
    assert preview.items == baseline.items
    with closing(connect_read_only(case.app)) as connection:
        assert saved_proposal_market_context(connection, saved) == fixed
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert service.publish(draft) == saved
    assert case.app.read_bytes() == before


@pytest.mark.parametrize(("microseconds", "changed"), [(-1, False), (0, True), (1, True)])
def test_market_reference_uses_publication_instant_cutoff_not_offset_or_day(
    tmp_path, microseconds, changed
):
    case = backend_case(tmp_path)
    _, _, saved = _publish(case)
    later = _later_document()
    AssessmentService(case.app).publish(later, expected_head=None)
    now = later.published_at.astimezone(UTC) + timedelta(microseconds=microseconds)
    preview = _preview(case, saved, now=now)
    assert preview.status == ("changed" if changed else "unchanged")
    assert [r.code for r in preview.change_reasons] == (
        ["market_assessment_revision_changed"] if changed else []
    )


def test_retry_of_the_same_fixed_market_reference_does_not_report_change(tmp_path):
    original = _market_document()
    case = _fixed_case(tmp_path, original)
    _, _, saved = _publish(case)
    AssessmentService(case.app).publish(original, expected_head=None)
    before = case.app.read_bytes()
    assert _preview(case, saved).status == "unchanged"
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("missing", [True, False])
def test_legacy_market_cutoff_is_not_inferred_from_publication(tmp_path, missing):
    case = backend_case(tmp_path)
    _, _, saved = _publish(case)
    later = _later_document()
    AssessmentService(case.app).publish(later, expected_head=None)
    legacy = later.payload()
    legacy["schema_version"] = 5
    if missing:
        legacy.pop("known_at")
    else:
        legacy["known_at"] = None
    with closing(sqlite3.connect(case.app)) as connection, connection:
        connection.execute(
            "UPDATE market_context_assessment SET schema_version=5,payload=? WHERE context_id=?",
            (json.dumps(legacy, ensure_ascii=False), later.context_id),
        )
    before = case.app.read_bytes()
    preview = _preview(case, saved)
    assert preview.status == "changed"
    assert "情報締切=未記録" in preview.change_reasons[0].message
    assert f"発行={later.published_at.isoformat()}" in preview.change_reasons[0].message
    assert preview.payload()["market_assessment_context"]["known_at"] is None
    assert case.app.read_bytes() == before


def test_broken_current_market_body_does_not_fall_back_or_block_exact_saved_read(tmp_path):
    case = backend_case(tmp_path)
    _, _, saved = _publish(case)
    later = _later_document()
    AssessmentService(case.app).publish(later, expected_head=None)
    with closing(sqlite3.connect(case.app)) as connection, connection:
        connection.execute(
            "UPDATE market_context_assessment SET payload='[]' WHERE context_id=?",
            (later.context_id,),
        )
    before = case.app.read_bytes()
    with pytest.raises(ValueError, match="assessment payload must be an object"):
        _preview(case, saved)
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert case.app.read_bytes() == before


def test_same_reference_exposes_saved_clock_without_claiming_new_research(tmp_path):
    original = _market_document()
    case = _fixed_case(tmp_path, original)
    _, _, saved = _publish(case)
    before = case.app.read_bytes()
    preview = _preview(case, saved)
    context = preview.payload()["market_assessment_context"]
    assert preview.status == "unchanged"
    assert context["same_reference"] is True
    assert context["context_id"] == context["saved_context_id"] == original.context_id
    assert context["as_of"] == original.as_of.isoformat()
    assert context["known_at"] == original.known_at.isoformat()
    assert context["published_at"] == original.published_at.isoformat()
    assert context["age_as_of_days"] == 0
    assert "新事実を再確認した証拠ではなく" in context["interpretation"]
    assert case.app.read_bytes() == before


def test_missing_market_clock_does_not_invent_same_reference_or_age(tmp_path):
    case = backend_case(tmp_path)
    _, _, saved = _publish(case)
    context = _preview(case, saved).payload()["market_assessment_context"]
    for key in (
        "context_id",
        "same_reference",
        "as_of",
        "known_at",
        "published_at",
        "age_as_of_days",
    ):
        assert context[key] is None


def test_market_age_is_jst_calendar_days_not_utc_or_publication_age():
    from baibai_engine.investment_judgment.market_assessment.models import StoredAssessment
    from baibai_engine.investment_judgment.trade_proposal.preview import _market_assessment_context

    record = StoredAssessment.from_payload(_market_document().payload())
    midnight = datetime.combine(record.as_of + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    context = _market_assessment_context(
        record, saved_context_id=record.context_id, now=midnight - timedelta(hours=9)
    )
    assert context["age_as_of_days"] == 1
    earlier = _market_assessment_context(
        record,
        saved_context_id=record.context_id,
        now=midnight - timedelta(hours=9, microseconds=1),
    )
    assert earlier["age_as_of_days"] == 0
