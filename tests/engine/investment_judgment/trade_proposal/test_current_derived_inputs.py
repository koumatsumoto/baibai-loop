"""Legacy exact identity is not permission to reuse inconsistent derived inputs today."""

import sqlite3
from contextlib import closing

import pytest
from tests.engine.investment_judgment.stock_assessment.test_stock_assessment import ratio_pair
from tests.helpers.assessment_v1 import assessment_id, review_id, update_review_hash
from tests.helpers.proposal import (
    PUBLISHED,
    backend_case,
    bind_review,
    read_saved_proposal,
    reviewed_proposal,
)

from baibai_engine.investment_judgment.independent_review.contracts import AssessmentReview
from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    UnpublishedAssessment,
    assessment_core_hash,
    evaluate_assessment,
)
from baibai_engine.investment_judgment.stock_assessment.store import load_reviewed_assessment
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalError,
    TradeProposal,
    proposal_draft_sha256,
)
from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.preview import preview_current_terms
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.primitives.json import canonical_json

FAULTS = ("negative_share_denominator", "metric_predates_input")


def _case(tmp_path, fault, *, holding=False, broken=False):
    case = backend_case(tmp_path, holding=holding, broken=broken)
    document, review = ratio_pair()
    review["review_id"] = review_id(4)
    if fault in {"negative_share_denominator", "signed_general_ratio", "unused_unknown"}:
        document["input_snapshot"]["facts"][-2]["value"] = -120_000_000
        document["input_snapshot"]["facts"][-1]["value"] = -1_000_000
    if fault == "metric_predates_input":
        document["derived"]["metrics"][0]["as_of"] = "2026-10-13"
    elif fault == "negative_profit":
        document["input_snapshot"]["facts"][-2]["value"] = -120_000_000
        document["derived"]["metrics"][0]["value"] = -120
    elif fault == "signed_general_ratio":
        document["input_snapshot"]["facts"][-1]["fact_kind"] = "other"
    elif fault == "unused_unknown":
        document["input_snapshot"]["facts"][-1]["value"] = "未確認"
        document["derived"]["metrics"] = []
    elif fault == "different_dates":
        document["input_snapshot"]["facts"][-2]["as_of"] = "2026-10-13"
    if broken:
        document["investment_case"].update(status="broken", status_reason="合成の事業仮説破綻")
        document["conclusion"]["stance"] = "unsupported"
    update_review_hash(document, review)
    typed = StockAssessmentDocument.model_validate(document)
    typed_review = AssessmentReview.model_validate(review)
    core = assessment_core_hash(typed)
    # Historical fixture only: old publication contracts admitted these two faults.
    # Do not weaken today's writer or mutate an existing immutable Assessment.
    with closing(sqlite3.connect(case.app)) as connection, connection:
        connection.execute(
            "INSERT INTO stock_assessment VALUES(?,?,?,?,?,?,?,?)",
            (
                assessment_id(3),
                "1234",
                "2026-10-14",
                typed.conclusion.stance,
                "2026-10-14T16:07:00+09:00",
                assessment_id(),
                canonical_json(typed.model_dump(mode="json")),
                core,
            ),
        )
        connection.execute(
            "INSERT INTO stock_assessment_review VALUES(?,?,?,?)",
            (
                typed_review.review_id,
                assessment_id(3),
                typed_review.reviewed_at.isoformat(),
                canonical_json(typed_review.model_dump(mode="json")),
            ),
        )
        case.pair = load_reviewed_assessment(connection, assessment_id(3))
    case.operations.record_assessment(
        case.operation.operation_id, case.pair.assessment_id, now=PUBLISHED
    )
    return case


def _sell_terms():
    return {
        "side": "sell",
        "limit_price_yen": "900",
        "quantity": 100,
        "notional_yen": "90000",
        "target_session": "2026-10-15",
        "valuation_as_of": "2026-10-14",
        "maximum_entry_price_yen": None,
        "price_grid_rule_id": "tse-common-conservative-20261014",
        "conditions": ["今回確認した回収理由と株式単位が変わらないこと"],
        "warnings": [],
    }


def _service(case):
    return TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)


def _legacy_proposal(case, draft):
    saved = TradeProposal.model_validate(
        {**draft.model_dump(mode="python"), "published_at": PUBLISHED}
    )
    digest = proposal_draft_sha256(draft)
    with closing(sqlite3.connect(case.app)) as connection, connection:
        connection.execute(
            "INSERT INTO investment_proposal VALUES(?,?,?,?,?,?,?)",
            (
                saved.proposal_id,
                saved.as_of.isoformat(),
                saved.prepared_at.isoformat(),
                saved.published_at.isoformat(),
                saved.request_scope,
                canonical_json(saved.payload()),
                digest,
            ),
        )
        connection.executemany(
            "INSERT INTO investment_proposal_item VALUES(?,?,?,?,?)",
            [
                (saved.proposal_id, i, item.ticker, item.action, item.assessment_ref.assessment_id)
                for i, item in enumerate(saved.items)
            ],
        )
        if saved.items[0].action == "buy":
            connection.execute(
                "INSERT INTO position_decision_reference VALUES(?,?,'buy','proposal',?,?,?)",
                (saved.proposal_id, "1234", saved.proposal_id, saved.proposal_id, digest),
            )
    return saved


@pytest.mark.parametrize("fault", FAULTS)
def test_old_inconsistent_inputs_cannot_be_used_for_a_new_buy(tmp_path, fault):
    case = _case(tmp_path, fault)
    pair = case.pair
    assert pair.evaluation.errors == ()
    assert evaluate_assessment(
        pair.document, review=pair.review, identity=UnpublishedAssessment.DRAFT, now=PUBLISHED
    ).errors
    draft = reviewed_proposal(case)
    before = case.app.read_bytes()
    checked = _service(case).check(draft)
    assert not checked.valid
    assert [reason.code for reason in checked.blockers] == ["valuation_inputs_require_refresh"]
    diagnostic = checked.calculations["1234"]
    assert diagnostic["quantity"] == 0
    assert diagnostic["current_price_projection"] is None
    assert diagnostic["limit_price_projection"] is None
    assert diagnostic["capital_comparison"] is None
    with pytest.raises(ProposalError, match="派生計算"):
        _service(case).publish(draft)
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("fault", FAULTS)
def test_legacy_exact_retry_survives_but_context_and_preview_do_not_reapply_arithmetic(
    tmp_path, fault
):
    case = _case(tmp_path, fault)
    draft = reviewed_proposal(case)
    saved = _legacy_proposal(case, draft)
    before = case.app.read_bytes()
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert _service(case).publish(draft) == saved
    context = build_proposal_context(
        operation_id=case.operation.operation_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED,
    )
    assert context.calculations["1234"]["current_quote_projection"] is None
    preview = preview_current_terms(
        proposal_id=saved.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED,
    )
    assert preview.status == "blocked"
    row = preview.items[0]
    assert row["saved_terms"] == saved.items[0].terms.model_dump(mode="json")
    assert row["saved_limit_revalidation"]["capital_comparison"] is None
    assert row["saved_limit_revalidation"]["quantity"] == 0
    assert row["current_quote_calculation"]["current_price_projection"] is None
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("fault", FAULTS)
@pytest.mark.parametrize("status", ["sufficient", "insufficient"])
def test_remaining_reward_cannot_reuse_invalid_legacy_arithmetic(tmp_path, fault, status):
    case = _case(tmp_path, fault, holding=True)
    raw = reviewed_proposal(case, action="hold").payload()
    item = raw["items"][0]
    item["remaining_reward"] = {"status": status, "reason": "合成の現在見返り判断"}
    if status == "sufficient":
        item["holding_basis"] = "sufficient_remaining_reward"
    else:
        item.pop("holding_basis")
        item.pop("return_calculation_unavailable_reason")
        item.update(
            action="sell",
            reason_code="insufficient_remaining_reward",
            terms=_sell_terms(),
            terms_unavailable_reasons=[],
        )
    draft = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    checked = _service(case).check(draft)
    assert not checked.valid
    assert "valuation_inputs_require_refresh" in {reason.code for reason in checked.blockers}
    assert checked.calculations["1234"]["current_price_projection"] is None


@pytest.mark.parametrize("action", ["no_action", "defer", "hold", "sell"])
def test_nonquantitative_decisions_are_not_forced_to_adopt_or_repair_values(tmp_path, action):
    case = _case(tmp_path, FAULTS[0], holding=action in {"hold", "sell"}, broken=action == "sell")
    draft = reviewed_proposal(case, action=action)
    if action == "sell":
        raw = draft.payload()
        raw["items"][0]["terms_unavailable_reasons"] = []
        raw["items"][0]["terms"] = _sell_terms()
        draft = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    checked = _service(case).check(draft)
    assert checked.valid
    if action in {"hold", "sell"}:
        assert checked.calculations["1234"]["current_price_projection"] is None
    saved = _service(case).publish(draft)
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved


@pytest.mark.parametrize(
    "kind", ["negative_profit", "signed_general_ratio", "unused_unknown", "different_dates"]
)
def test_existing_signed_and_unknown_observations_do_not_become_new_gates(tmp_path, kind):
    case = _case(tmp_path, kind)
    draft = reviewed_proposal(case)
    checked = _service(case).check(draft)
    assert checked.valid
    assert checked.calculations["1234"]["quantity"] == 300
    assert checked.calculations["1234"]["capital_comparison"] is not None
