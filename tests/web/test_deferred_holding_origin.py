"""保留の再開に必要な当初仮説。合成の通常保存と固定表示の境界を分ける。"""

import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest
from tests.engine.investment_judgment.trade_proposal.test_original_buy_records_context import (
    NOW,
    _setup,
)
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.proposal import (
    backend_case,
    bind_review,
    read_saved_proposal,
)
from tests.web.test_saved_deferred_holding import _saved

from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.contracts import TradeProposal
from baibai_engine.investment_judgment.trade_proposal.scaffold import scaffold_proposal
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_web.readmodel.builders.proposal import (
    build_latest_proposal,
    build_proposal_history,
    project_saved_proposal,
)
from baibai_web.readmodel.proposal_models import SavedDeferItemView
from baibai_web.sources.proposal import DbProposalSource

DEFER_ID = "investment-proposal-00000000-0000-4000-8000-000000000021"


def _published_defer(tmp_path, *, unreviewed=False):
    fixture = _setup(tmp_path)
    case, original = fixture.case, fixture.case.pair
    document, review = pair_payload(review_number=11)
    document["investment_case"]["explanation"] = "今回の素材。購入時の理由ではない。"
    document["valuation"] = {
        "status": "unresolved",
        "market_price_fact_id": None,
        "horizon_months": None,
        "required_annual_return_pct": None,
        "base": None,
        "downside": None,
        "unresolved_reason": "合成: 借換期限の影響を未確認。",
    }
    document["conclusion"]["stance"] = "uncertain"
    review["recalculated_projections"] = []
    update_review_hash(document, review)
    current = AssessmentStoreService(
        case.app, clock=lambda: NOW - timedelta(minutes=5)
    ).publish_reviewed_assessment(
        assessment_id(10), document, review, supersedes_id=original.assessment_id
    )
    if unreviewed:
        case.operations.record_unfinished(
            fixture.operation.operation_id,
            ticker="1234",
            reason="合成: 当初の反証条件を再確認中。",
            references=("fixture:original-thesis",),
            now=NOW,
        )
    else:
        case.operations.record_assessment(
            fixture.operation.operation_id, current.assessment_id, now=NOW
        )
    raw = scaffold_proposal(
        operation_id=fixture.operation.operation_id,
        proposal_id=DEFER_ID,
        db_path=case.app,
        sqlite_path=case.market,
        now=NOW,
    )
    raw.update(
        headline="合成の保有判断保留",
        comparison="当初仮説の成立条件を再確認してから比較する。",
        forgone="未確認のまま保有/売却を採択しない。",
    )
    raw["items"][0].update(
        rationale="当初の借換仮説と今回の確認結果を区別する。",
        resume_conditions=["当初仮説の反証条件と後発資料を照合する。"],
    )
    raw["review"].update(
        reviewer_identity="synthetic-independent-reviewer", reviewed_at=NOW.isoformat()
    )
    draft = bind_review(raw, {current.assessment_id: current.document})
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: NOW + timedelta(minutes=1)
    )
    assert service.check(draft).valid
    saved = service.publish(draft)
    return case, original, current, service, draft, saved


@pytest.mark.parametrize("unreviewed", [False, True])
def test_normal_saved_defer_keeps_exact_original_thesis_separate_from_current(
    tmp_path, monkeypatch, unreviewed
):
    case, original, current, service, draft, saved = _published_defer(
        tmp_path, unreviewed=unreviewed
    )
    document, review = pair_payload(review_number=31)
    document["investment_case"]["explanation"] = "後発評価だけの理由"
    update_review_hash(document, review)
    AssessmentStoreService(
        case.app, clock=lambda: NOW + timedelta(minutes=2)
    ).publish_reviewed_assessment(
        assessment_id(30), document, review, supersedes_id=current.assessment_id
    )
    before = case.app.read_bytes()
    view = DbProposalSource(case.app).latest()
    item = view.items[0]
    origin = item.model_dump(mode="json").get("saved_holding_origin")
    assert origin is not None
    assert origin["assessment_ref"] == saved.inputs.positions[0].original_assessment_ref.model_dump(
        mode="json"
    )
    assert origin["assessment"]["assessment_id"] == original.assessment_id
    assert origin["assessment"]["core_sha256"] == original.core_sha256
    assert origin["assessment"]["review_id"] == original.review.review_id
    assert origin["assessment"]["invalidation_conditions"] == list(
        original.document.investment_case.invalidation_conditions
    )
    assert (
        origin["assessment"]["strongest_countercase"]
        == original.document.conclusion.strongest_countercase
    )
    assert origin["investment_reason"] is origin["reason_unavailable"] is None
    assert (item.assessment is None) == unreviewed
    if not unreviewed:
        assert item.assessment.assessment_id == current.assessment_id
    assert "後発評価だけ" not in item.model_dump_json()
    assert item.action == saved.items[0].action == "defer"
    assert item.resume_conditions == list(saved.items[0].resume_conditions)
    assert "terms" not in item.model_dump()
    assert service.publish(draft) == saved
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert case.app.read_bytes() == before
    case.market.rename(tmp_path / "not-needed-market.sqlite")
    case.runs.rename(tmp_path / "not-needed-runs.sqlite")
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.trade_proposal.inputs.collect_proposal_inputs",
        lambda *_a, **_k: pytest.fail("not a current input read"),
    )
    assert DbProposalSource(case.app).latest() == view
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("unreviewed", [False, True])
@pytest.mark.parametrize("damage", ["missing", "core_hash", "review"])
def test_broken_stored_origin_is_unavailable_not_an_optional_display_gap(
    tmp_path, damage, unreviewed
):
    case, original, _current, _service, _draft, saved = _published_defer(
        tmp_path, unreviewed=unreviewed
    )
    # Damage the synthetic store before the real owner reader, not only the
    # presentation alias after the owner has already verified the same pair.
    with closing(sqlite3.connect(case.app)) as connection, connection:
        if damage == "missing":
            connection.execute(
                "DELETE FROM stock_assessment WHERE assessment_id=?", (original.assessment_id,)
            )
        elif damage == "core_hash":
            connection.execute("DROP TRIGGER stock_assessment_immutable")
            connection.execute(
                "UPDATE stock_assessment SET core_sha256=? WHERE assessment_id=?",
                ("f" * 64, original.assessment_id),
            )
        else:
            connection.execute(
                "DELETE FROM stock_assessment_review WHERE assessment_id=?",
                (original.assessment_id,),
            )
    before = case.app.read_bytes()
    latest = build_latest_proposal(DbProposalSource(case.app), generated_at=NOW)
    assert latest.status == "unavailable"
    assert latest.proposal is None
    assert latest.error == "保存済み提案を読み取れません。"
    history = build_proposal_history(DbProposalSource(case.app), generated_at=NOW)
    assert saved.proposal_id in history.unavailable_proposal_ids
    assert all(row.proposal_id != saved.proposal_id for row in history.proposals)
    for view in (latest, history):
        assert str(case.app) not in view.model_dump_json()
    assert case.app.read_bytes() == before


def test_normal_unlinked_holding_defer_preserves_recorded_reason_unavailability(tmp_path):
    case = backend_case(tmp_path, holding=True, valuation_unresolved=True)
    _service, _draft, saved = _saved(case, unreviewed=True)
    before = case.app.read_bytes()
    origin = (
        DbProposalSource(case.app)
        .latest()
        .items[0]
        .model_dump(mode="json")
        .get("saved_holding_origin")
    )
    assert origin == {
        "assessment_ref": None,
        "assessment": None,
        "investment_reason": None,
        "reason_unavailable": saved.inputs.positions[0].original_reason_unavailable_reason,
    }
    assert case.app.read_bytes() == before


def test_historical_prose_is_not_replaced_by_current_assessment(tmp_path):
    case = backend_case(tmp_path, holding=True, valuation_unresolved=True)
    _service, _draft, saved = _saved(case)
    raw = saved.payload()
    text = "当初理由 <script>確認待ち</script>\n" + "借換と回収。" * 100 + "末尾"
    raw["inputs"]["positions"][0].update(
        original_investment_reason=text, original_reason_unavailable_reason=None
    )
    historical = TradeProposal.model_validate(raw)
    before = historical.model_dump_json()
    origin = (
        project_saved_proposal(historical, {case.pair.assessment_id: case.pair})
        .items[0]
        .model_dump(mode="json")
        .get("saved_holding_origin")
    )
    assert origin == {
        "assessment_ref": None,
        "assessment": None,
        "investment_reason": text,
        "reason_unavailable": None,
    }
    assert historical.model_dump_json() == before


@pytest.mark.parametrize("mode", ["absent_position", "other_ticker", "new_scope"])
def test_origin_is_only_the_same_saved_holding_not_another_ticker_or_scope(tmp_path, mode):
    case = backend_case(tmp_path, holding=True, valuation_unresolved=True)
    _service, _draft, saved = _saved(case)
    raw = saved.payload()
    if mode == "absent_position":
        raw["inputs"]["positions"] = []
    elif mode == "other_ticker":
        raw["inputs"]["positions"][0]["ticker"] = "5678"
    else:
        raw["request_scope"] = "specified_stocks"
        for group in ("targets", "outcomes"):
            raw["research_scope"][group][0]["purpose"] = "new"
    historical = TradeProposal.model_validate(raw)
    assert (
        project_saved_proposal(historical, {case.pair.assessment_id: case.pair})
        .items[0]
        .model_dump(mode="json")
        .get("saved_holding_origin")
        is None
    )


@pytest.mark.parametrize("mode", ["absent", "null"])
def test_old_defer_payload_does_not_backfill_original_reason(tmp_path, mode):
    case = backend_case(tmp_path, holding=True, valuation_unresolved=True)
    _saved(case)
    raw = DbProposalSource(case.app).latest().items[0].model_dump(mode="json")
    raw["saved_holding_origin"] = None
    if mode == "absent":
        raw.pop("saved_holding_origin")
    assert SavedDeferItemView.model_validate(raw).saved_holding_origin is None


@pytest.mark.parametrize("field", ["core_sha256", "review_id"])
def test_original_reference_mismatch_is_unavailable_not_current_pair(tmp_path, field):
    _case, original, current, _service, _draft, saved = _published_defer(tmp_path)
    raw = saved.payload()
    reference = raw["inputs"]["positions"][0]["original_assessment_ref"]
    reference[field] = "f" * 64 if field == "core_sha256" else current.review.review_id
    historical = TradeProposal.model_validate(raw)
    before = historical.model_dump_json()
    item = project_saved_proposal(
        historical, {original.assessment_id: original, current.assessment_id: current}
    ).items[0]
    assert item.saved_holding_origin.assessment_ref.model_dump(mode="json") == reference
    assert item.saved_holding_origin.assessment is None
    assert item.assessment.assessment_id == current.assessment_id
    assert item.action == "defer"
    assert historical.model_dump_json() == before
