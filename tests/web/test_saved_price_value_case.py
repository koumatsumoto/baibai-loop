"""普通株一株の当時判断とその資本の行動を、提案の正規書込→exact読取で分離する。"""

from decimal import Decimal

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.proposal import (
    PREPARED,
    PROPOSAL_ID,
    PUBLISHED,
    backend_case,
    bind_review,
    reviewed_proposal,
)

from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.contracts import ProposalError
from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.preview import preview_current_terms
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_web.sources.proposal import read_proposal_views


def bind_finding(case, status, *, condition=None, last_acquisition="not_time_limited"):
    document, review = pair_payload(review_number=85)
    document["input_snapshot"]["facts"].append(
        dict(
            fact_id="right",
            fact_kind="other",
            value="取得者への給付は先順位分控除後。",
            unit="権利条件",
            as_of="2026-10-14",
            source_ids=["annual"],
        )
    )
    document["price_value_case"] = dict(
        status=status,
        value_fact_ids=["right"],
        price_compatible_conditions="回収期限の延長ならこの値段を説明できる。",
        price_only_condition=condition,
    )
    if last_acquisition != "not_time_limited":
        document["input_snapshot"]["facts"].append(
            dict(
                fact_id="cutoff",
                fact_kind="other" if last_acquisition is None else "last_eligible_trade_date",
                value=last_acquisition or "新取得者の給付資格は後続原典で確認が必要。",
                unit="notice" if last_acquisition is None else "date",
                as_of="2026-10-14",
                source_ids=["annual"],
            )
        )
        document["price_value_case"]["value_fact_ids"].append("cutoff")
        document["price_value_case"]["acquisition_limit"] = {
            "value_fact_id": "cutoff",
            "last_eligible_trade_date": last_acquisition,
        }
    update_review_hash(document, review)
    case.pair = AssessmentStoreService(
        case.app, clock=lambda: PREPARED
    ).publish_reviewed_assessment(
        assessment_id(85), document, review, supersedes_id=case.pair.assessment_id
    )
    case.operations.record_assessment(
        case.operation.operation_id, case.pair.assessment_id, now=PREPARED
    )


def context(case):
    return build_proposal_context(
        proposal_id=PROPOSAL_ID,
        operation_id=case.operation.operation_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PREPARED,
    ).calculations["1234"]


def service(case):
    return TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)


def buy_at(case, limit, saved_max):
    proposal = reviewed_proposal(case, action="buy").model_dump(mode="json")
    proposal["items"][0]["terms"].update(
        limit_price_yen=str(limit),
        quantity=100,
        notional_yen=str(100 * limit),
        maximum_entry_price_yen=str(saved_max),
    )
    return bind_review(proposal, {case.pair.assessment_id: case.pair.document})


def test_price_only_bound_is_not_a_text_hint_or_automatic_order(tmp_path):
    case = backend_case(tmp_path)
    bind_finding(
        case,
        "not_supported_at_quote",
        condition={"explanation": "事業・取得権は同一。", "ceiling_yen": "750"},
    )
    calculations = context(case)
    assert calculations["maximum_common_grid_price_yen"] == "750"
    assert calculations["buy_at_actual_quote"]["quantity"] == 0
    assert Decimal(calculations["calculation_at_maximum_price"]["maximum_price_yen"]) == 750
    invalid = buy_at(
        case, 850, 1000
    )  # Base/h は1000だが、本人が買える権利を支持できるのは750以下。
    check = service(case).check(invalid)
    assert not check.valid
    assert "limit_above_reviewed_price_condition" in {reason.code for reason in check.blockers}
    assert check.calculations["1234"]["maximum_quantity"] is None
    assert check.calculations["1234"]["capital_comparison"] is None
    with pytest.raises(ProposalError, match="同一権利"):
        service(case).publish(invalid)
    saved = service(case).publish(buy_at(case, 750, 750))
    before = case.app.read_bytes()
    view = read_proposal_views(case.app).latest.proposal.items[0]
    finding = view.assessment.price_value_case
    assert view.action == "buy"
    assert view.terms.limit_price_yen == "750"
    assert finding.status == "not_supported_at_quote"
    assert finding.price_only_condition.ceiling_yen == 750
    assert finding.observed_quote.price_yen == 1100
    assert finding.value_facts[0].value == "取得者への給付は先順位分控除後。"
    assert {s.source_id for s in finding.sources} == {"price", "annual"}
    preview = preview_current_terms(
        proposal_id=saved.proposal_id, db_path=case.app, sqlite_path=case.market, now=PUBLISHED
    ).payload()
    item = preview["items"][0]
    assert item["saved_limit_revalidation"]["quantity"] == 100
    assert item["current_quote_calculation"]["quantity"] == 0
    assert case.app.read_bytes() == before


@pytest.mark.parametrize(
    ("status", "condition", "reason"),
    [
        ("undetermined", None, "price_value_undetermined"),
        ("not_supported_at_quote", None, "price_value_not_supported"),
        (
            "not_supported_at_quote",
            {"explanation": "75円でも権利を再確認。", "ceiling_yen": None},
            "price_value_not_supported",
        ),
    ],
)
def test_missing_research_or_a_text_only_price_never_becomes_an_order(
    tmp_path, status, condition, reason
):
    case = backend_case(tmp_path)
    bind_finding(case, status, condition=condition)
    row = context(case)
    assert "maximum_common_grid_price_yen" not in row
    buy = row["buy_at_actual_quote"]
    assert buy["quantity"] == 0
    assert buy["maximum_quantity"] is None
    assert buy["capital_comparison"] is None
    assert buy["maximum_price_yen"] is None
    assert reason in {b["code"] for b in buy["hard_blockers"]}
    service(case).publish(reviewed_proposal(case, action="defer"))
    view = read_proposal_views(case.app).latest.proposal.items[0]
    assert view.assessment.price_value_case.status == status
    assert view.action == "defer"


def test_no_usable_quantity_above_base_cap_even_when_the_reviewed_price_is_higher(tmp_path):
    case = backend_case(tmp_path)
    bind_finding(case, "supported_at_quote")
    invalid = buy_at(case, 1050, 1100)  # 支持価格1100、Baseの上限1000、余力はある。
    checked = service(case).check(invalid)
    assert not checked.valid
    reasons = {reason.code for reason in checked.blockers}
    assert "limit_above_pmax" in reasons
    assert "limit_above_reviewed_price_condition" not in reasons
    calculation = checked.calculations["1234"]
    assert Decimal(calculation["maximum_price_yen"]) == 1000
    assert calculation["quantity"] == 0
    assert calculation["maximum_quantity"] is None
    assert calculation["capital_comparison"] is None
    assert service(case).check(buy_at(case, 1000, 1000)).valid


def test_inadequate_cash_changes_allocation_not_the_reviewed_per_share_finding(tmp_path):
    case = backend_case(tmp_path, cash_yen=500)
    bind_finding(case, "supported_at_quote")
    row = context(case)["calculation_at_maximum_price"]
    assert row["quantity"] == 0
    assert "available_cash_below_board_lot" in {b["code"] for b in row["hard_blockers"]}
    service(case).publish(reviewed_proposal(case, action="no_action"))
    view = read_proposal_views(case.app).latest.proposal.items[0]
    assert view.action == "no_action"
    assert view.assessment.price_value_case.status == "supported_at_quote"


@pytest.mark.parametrize(
    ("eligible_until", "expected"),
    [
        ("2026-10-14", "right_acquisition_expired"),
        (None, "right_acquisition_unconfirmed"),
        ("2026-10-15", None),
    ],
)
def test_next_order_session_controls_entitlement_not_unchanged_quote_or_payment_date(
    tmp_path, eligible_until, expected
):
    case = backend_case(tmp_path)
    bind_finding(case, "supported_at_quote", last_acquisition=eligible_until)
    row = context(case)
    if expected is None:
        assert row["current_quote_projection"] is not None
        check = service(case).check(buy_at(case, 1000, 1000))
        assert check.valid
    else:
        assert row["current_quote_projection"] is None
        assert row["buy_at_actual_quote"]["current_price_projection"] is None
        assert row["buy_at_actual_quote"]["quantity"] == 0
        check = service(case).check(buy_at(case, 1000, 1000))
        assert expected in {reason.code for reason in check.blockers}
    saved = service(case).publish(reviewed_proposal(case, action="no_action"))
    preview = preview_current_terms(
        proposal_id=saved.proposal_id, db_path=case.app, sqlite_path=case.market, now=PUBLISHED
    ).payload()["items"][0]
    record = read_proposal_views(case.app).latest.proposal.items[0].assessment.price_value_case
    assert record.status == "supported_at_quote"
    assert (
        record.acquisition_limit.model_dump(mode="json")["last_eligible_trade_date"]
        == eligible_until
    )
    if expected is not None:
        assert preview["current_quote_calculation"] is None
        assert preview["current_quote_unavailable_reason"]["code"] == expected
    else:
        assert preview["current_quote_calculation"] is not None
