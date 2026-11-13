"""保存提案投影。合成本文の意味とread-only境界を確認する。"""

import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload
from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal

from baibai_engine.investment_judgment.independent_review.contracts import AssessmentReview
from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    assessment_core_hash,
    evaluate_assessment,
)
from baibai_engine.investment_judgment.stock_assessment.store import (
    AssessmentStoreService,
    ReviewedAssessment,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import TradeProposal
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.storage.sqlite.application.write import initialize_database
from baibai_web.readmodel.builders.proposal import build_latest_proposal, project_saved_proposal
from baibai_web.sources import proposal as proposal_source
from baibai_web.sources.proposal import DbProposalSource


@pytest.fixture
def payload():
    return json.loads(Path("tests/fixtures/domain-assessment-proposal-synthetic.json").read_text())[
        "investment_proposal"
    ]


def _pair(
    *,
    number=1,
    explanation=None,
    partial=False,
    perspectives=(),
    price=None,
    valuation=None,
    risks=None,
    questions=(),
    acceptance=None,
):
    raw, review_raw = pair_payload(review_number=number + 1)
    raw["model_perspectives"] = list(perspectives)
    if risks is not None:
        raw["permanent_loss_risks"] = risks
    raw["findings"]["unresolved_questions"] = list(questions)
    if acceptance is not None:
        review_raw["uncertainty_acceptance_reason"] = acceptance
    if price is not None:
        raw["input_snapshot"]["facts"][0]["value"] = price
    if valuation is not None:
        raw["valuation"] = valuation
        review_raw["recalculated_projections"] = (
            [
                {"name": name, **{k: v for k, v in valuation[name].items() if k.endswith("_yen")}}
                for name in ("base", "downside")
            ]
            if valuation["status"] == "resolved"
            else []
        )
    if explanation is not None:
        raw["investment_case"]["explanation"] = explanation
    if partial:
        raw["conclusion"]["stance"] = "uncertain"
        raw["investment_case"]["status"] = "uncertain"
        review_raw["primary_source_check"] = "partially_verified"
        review_raw["uncertainty_acceptance_reason"] = "未確認範囲は残るため具体条件を保留する。"
    document = StockAssessmentDocument.model_validate(raw)
    identity = assessment_core_hash(document)
    review_raw["reviewed_assessment_sha256"] = identity
    review = AssessmentReview.model_validate(review_raw)
    evaluation = evaluate_assessment(document, review=review, identity=identity, now=PUBLISHED)
    return ReviewedAssessment(
        assessment_id(number), document, review, identity, evaluation, published_at=PUBLISHED
    )


def _ref(pair):
    return {
        "assessment_id": pair.assessment_id,
        "core_sha256": pair.core_sha256,
        "review_id": pair.review.review_id,
    }


def _current(payload, pair):
    payload["items"][0]["assessment_ref"] = _ref(pair)
    payload["research_scope"]["outcomes"][0]["assessment_ref"] = _ref(pair)


def _holding(payload, *, action, original):
    position = {
        "ticker": "1234",
        "sector": "情報・通信業",
        "common_factors": ["domestic-demand"],
        "quantity": 100,
        "cost_yen": "90000",
        "quantity_basis_confirmed": True,
        "original_assessment_ref": None,
        "original_investment_reason": None,
        "original_reason_unavailable_reason": None,
        **original,
    }
    payload["request_scope"] = "review_positions"
    payload["research_scope"]["targets"][0]["purpose"] = "holding"
    payload["research_scope"]["outcomes"][0]["purpose"] = "holding"
    payload["inputs"]["positions"] = [position]
    item = payload["items"][0]
    for field in ("terms", "terms_unavailable_reasons", "timing", "proposed_limit_yen"):
        item.pop(field)
    item.update(
        action=action,
        position=position,
        case_reassessment_reason="今回の根拠で当初仮説の変化を検討した。",
        remaining_reward=None,
    )
    if action == "hold":
        item.update(
            holding_basis="investment_case_intact",
            return_calculation_unavailable_reason="旧日起点で当日returnは未計算。",
        )
    else:
        item.update(
            reason_code="economic_invalidation",
            terms=None,
            terms_unavailable_reasons=[{"code": "quote_missing_or_stale", "message": "価格未確認"}],
        )


def test_saved_buy_preserves_clocks_conditions_precision_warnings_and_exposure(payload):
    pair = _pair()
    _current(payload, pair)
    payload["warnings"] = ["保存警告の全文"]
    payload["items"][0]["rationale"] = "保存理由の一行目\n二行目"
    proposal = TradeProposal.model_validate(payload)
    view = project_saved_proposal(proposal, {pair.assessment_id: pair})
    item = view.items[0]
    assert view.proposal_id == proposal.proposal_id
    assert view.as_of == proposal.as_of
    assert view.published_at == proposal.published_at
    assert view.prepared_at == proposal.prepared_at
    assert (view.headline, view.comparison, view.forgone) == (
        proposal.headline,
        proposal.comparison,
        proposal.forgone,
    )
    assert item.rationale == "保存理由の一行目\n二行目"
    assert item.review_conditions == list(proposal.items[0].review_conditions)
    assert item.saved_quote.price_yen == "1100"
    assert item.terms.limit_price_yen == "1000"
    assert item.terms.notional_yen == "300000"
    assert item.terms.target_session.isoformat() == "2026-10-15"
    assert item.terms.conditions == list(proposal.items[0].terms.conditions)
    assert item.terms.warnings == list(proposal.items[0].terms.warnings)
    assert view.warnings == ["保存警告の全文"]
    assert view.portfolio_exposure.ticker.prospective_pct == "100"
    assert item.liquidity.adv_yen is None
    assert item.liquidity.price_window_end is None
    assert item.liquidity.unavailable_reason == proposal.liquidity_context[0].unavailable_reason
    assert not {"inputs", "review", "research_scope", "current_eligibility"} & view.model_fields_set


def test_model_perspectives_project_exact_judgments_sources_and_missingness(payload):
    rows = [
        {
            "model": "company_financials",
            "assessment": "mixed",
            "summary": "財務の支持と相反を同時に残す。\n0は明示値、欠測は不明。",
            "source_ids": ["annual"],
        },
        {
            "model": "industry_trends",
            "assessment": "insufficient",
            "summary": "原本はあるが対象業界の意味抽出は未対応。変化なしとはしない。",
            "source_ids": [],
        },
    ]
    pair = _pair(perspectives=rows)
    _current(payload, pair)
    before = pair.document.model_dump_json()
    view = project_saved_proposal(TradeProposal.model_validate(payload), {pair.assessment_id: pair})
    assessment = view.items[0].assessment
    assert [row.model_dump(mode="json") for row in assessment.model_perspectives] == rows
    expected = [
        s.model_dump(mode="json", exclude={"ticker"})
        for s in pair.document.input_snapshot.sources
        if s.source_id == "annual"
    ]
    assert [s.model_dump(mode="json") for s in assessment.model_perspective_sources] == expected
    assert pair.document.model_dump_json() == before
    assert len(assessment.model_perspectives) == 2  # Other five are absent, not neutral/zero.


def test_legacy_model_perspectives_remain_unrecorded(payload):
    pair = _pair()
    _current(payload, pair)
    view = project_saved_proposal(TradeProposal.model_validate(payload), {pair.assessment_id: pair})
    assert view.items[0].assessment.model_perspectives == []
    assert view.items[0].assessment.model_perspective_sources == []


def test_risks_and_questions_keep_exact_basis_without_changing_buy_or_value(payload):
    raw, _ = pair_payload()
    risks = raw["permanent_loss_risks"]
    for risk in risks:
        if risk["axis"] == "debt_repayment":
            risk.update(
                assessment="adverse",
                evidence_status="partially_verified",
                summary="合成：返済期日は確認。借換は未確約。cashと負債を同時に動かす。",
                as_of="2026-09-30",
                source_ids=["annual"],
            )
        elif risk["axis"] == "cash_flow":
            risk.update(
                assessment="unknown",
                summary="期末黒字でも支払日前の資金が足りるとは限らない。",
            )
    questions = [
        "将来CFは未確定。会社の配当支出と新規買付者の受取権利を分けて確認する。",
        "未決定の還元と元本返済へ同じcashを二重使用しないか。",
    ]
    pair = _pair(
        risks=risks,
        questions=questions,
        acceptance="合成の引受理由：借換未確約を残し、現金返済する代替経路と見返りを比較した。",
    )
    assert not pair.evaluation.errors
    _current(payload, pair)
    proposal = TradeProposal.model_validate(payload)
    before_document, before_proposal = pair.document.model_dump_json(), proposal.model_dump_json()
    item = project_saved_proposal(proposal, {pair.assessment_id: pair}).items[0]
    projected = item.assessment.risk_analysis
    assert [risk.model_dump(mode="json") for risk in projected.risks] == risks
    assert projected.unresolved_questions == questions
    referenced = {source for risk in risks for source in risk["source_ids"]}
    assert [source.model_dump(mode="json") for source in projected.sources] == [
        source.model_dump(mode="json", exclude={"ticker"})
        for source in pair.document.input_snapshot.sources
        if source.source_id in referenced
    ]
    assert item.action == "buy"  # This projection does not reinterpret unknown/adverse as a gate.
    assert item.assessment.model_perspectives == []  # No inferred cross-model opinions.
    assert item.assessment.core_sha256 == pair.core_sha256
    assert item.assessment.valuation.base.calculation == pair.document.valuation.base.calculation
    assert pair.document.model_dump_json() == before_document
    assert proposal.model_dump_json() == before_proposal


def test_old_serving_payload_omission_differs_from_saved_empty_questions(payload):
    from baibai_web.readmodel.proposal_models import SavedAssessmentView

    pair = _pair()
    _current(payload, pair)
    assessment = (
        project_saved_proposal(TradeProposal.model_validate(payload), {pair.assessment_id: pair})
        .items[0]
        .assessment
    )
    assert len(assessment.risk_analysis.risks) == 7
    assert assessment.risk_analysis.unresolved_questions == []
    old = assessment.model_dump(mode="json")
    del old["risk_analysis"]
    assert SavedAssessmentView.model_validate(old).risk_analysis is None


def test_saved_valuation_uses_assessment_price_and_keeps_assumptions_without_mutation(payload):
    raw, _ = pair_payload()
    raw["valuation"]["base"]["terminal_value_per_share_yen"] = "1100.0000000000000001"
    pair = _pair(price=1000, valuation=raw["valuation"])
    assert not pair.evaluation.errors
    _current(payload, pair)
    proposal = TradeProposal.model_validate(payload)
    before = pair.document.model_dump_json()
    view = project_saved_proposal(proposal, {pair.assessment_id: pair})
    item = view.items[0]
    value = item.assessment.valuation
    assert value.status == "resolved"
    assert value.price_yen == "1000"
    assert item.saved_quote.price_yen == "1100"
    assert item.terms.limit_price_yen == "1000"
    assert value.price_as_of == pair.document.input_snapshot.facts[0].as_of
    assert value.price_observed_at == pair.document.input_snapshot.facts[0].observed_at
    assert value.price_basis == "last_close_unadjusted"
    assert value.horizon_months == 12
    assert value.required_annual_return_pct == "10"
    assert value.base.annualized_return_pct == "10.0000"
    assert value.downside.annualized_return_pct == "-30.0000"
    for name in ("base", "downside"):
        original = getattr(pair.document.valuation, name)
        projected = getattr(value, name)
        assert projected.terminal_value_per_share_yen == str(original.terminal_value_per_share_yen)
        assert projected.cash_distribution_per_share_yen == "0"
        assert projected.calculation == original.calculation
    assert pair.document.model_dump_json() == before
    assert item.assessment.core_sha256 == pair.core_sha256


def test_saved_valuation_zero_is_a_loss_not_missing(payload):
    raw, _ = pair_payload()
    raw["valuation"]["downside"]["terminal_value_per_share_yen"] = "0"
    pair = _pair(valuation=raw["valuation"])
    assert not pair.evaluation.errors
    _current(payload, pair)
    value = (
        project_saved_proposal(TradeProposal.model_validate(payload), {pair.assessment_id: pair})
        .items[0]
        .assessment.valuation
    )
    assert value.downside.terminal_value_per_share_yen == "0"
    assert value.downside.cash_distribution_per_share_yen == "0"
    assert value.downside.annualized_return_pct == "-100.0000"


def test_saved_unresolved_valuation_keeps_its_reason_without_invented_returns(payload):
    pair = _pair(
        partial=True,
        valuation={
            "status": "unresolved",
            "market_price_fact_id": None,
            "horizon_months": None,
            "required_annual_return_pct": None,
            "base": None,
            "downside": None,
            "unresolved_reason": "回収時期の幅を定められず、価値評価は未確定。",
        },
    )
    assert not pair.evaluation.errors
    _current(payload, pair)
    item = payload["items"][0]
    for field in ("terms", "terms_unavailable_reasons", "timing", "proposed_limit_yen"):
        item.pop(field)
    item.update(
        action="defer",
        blockers=[{"code": "unresolved", "message": "回収時期未確定"}],
        resume_conditions=["回収時期を確認"],
    )
    view = project_saved_proposal(TradeProposal.model_validate(payload), {pair.assessment_id: pair})
    assert view.items[0].assessment.valuation.model_dump() == {
        "status": "unresolved",
        "reason": pair.document.valuation.unresolved_reason,
    }


@pytest.mark.parametrize("window_end", [None, "2026-10-07"])
@pytest.mark.parametrize("with_window", [False, True])
def test_saved_generated_liquidity_keeps_window_end_separate_from_analysis_date(
    payload, window_end, with_window
):
    pair = _pair()
    _current(payload, pair)
    window = {
        "start_date": "2026-09-10",
        "end_date": "2026-10-07",
        "price_bar_count": 20,
        "valid_turnover_count": 19,
        "median_yen": "100000.000000001",
    }
    payload["liquidity_context"] = [
        {
            "ticker": "1234",
            "generation_id": "synthetic",
            "analysis_as_of": "2026-10-14",
            "calculation_revision": "synthetic-v1",
            "adv_yen": "100000000.000000001",
            "unavailable_reason": None,
            **({"price_window_end": window_end} if window_end is not None else {}),
            **({"turnover_window": window} if with_window else {}),
        }
    ]
    proposal = TradeProposal.model_validate(payload)
    before = proposal.model_dump_json()
    row = project_saved_proposal(proposal, {pair.assessment_id: pair}).items[0].liquidity
    assert row.model_dump(mode="json") == {
        "analysis_as_of": "2026-10-14",
        "price_window_end": window_end,
        "turnover_window": window if with_window else None,
        "adv_yen": "100000000.000000001",
        "unavailable_reason": None,
    }
    assert proposal.model_dump_json() == before


@pytest.mark.parametrize("timing", ["wait_limit", "place_limit"])
def test_terms_absent_buy_keeps_exact_author_limit_without_invented_order_terms(payload, timing):
    pair = _pair()
    _current(payload, pair)
    payload["items"][0].update(
        timing=timing,
        terms=None,
        proposed_limit_yen="999.1234567890123456789",
        terms_unavailable_reasons=[
            {"code": "price_grid_rule_unavailable", "message": "呼値未確認のため具体条件未確定"}
        ],
    )
    view = project_saved_proposal(TradeProposal.model_validate(payload), {pair.assessment_id: pair})
    item = view.items[0]
    assert item.action == "buy"
    assert item.timing == timing
    assert item.terms is None
    assert item.proposed_limit_yen == "999.1234567890123456789"
    assert item.terms_unavailable_reasons[0].message == "呼値未確認のため具体条件未確定"


@pytest.mark.parametrize("action", ["hold", "sell"])
@pytest.mark.parametrize("original_kind", ["reference", "prose", "unavailable"])
def test_holding_keeps_original_reason_separate_from_current_assessment(
    payload, action, original_kind
):
    original, current, newer = (
        _pair(
            number=n,
            explanation=f"評価{n}の理由",
            questions=[f"評価{n}に残った問い"],
            acceptance=f"評価{n}の合成の引受理由。",
        )
        for n in (1, 10, 20)
    )
    origin = {
        "reference": {"original_assessment_ref": _ref(original)},
        "prose": {"original_investment_reason": "当初の保存理由"},
        "unavailable": {"original_reason_unavailable_reason": "当初資料を取得できない"},
    }[original_kind]
    _current(payload, current)
    _holding(payload, action=action, original=origin)
    pairs = {pair.assessment_id: pair for pair in (original, current, newer)}
    item = project_saved_proposal(TradeProposal.model_validate(payload), pairs).items[0]
    assert item.action == action
    assert item.assessment.assessment_id == current.assessment_id
    assert item.assessment.investment_case_explanation == "評価10の理由"
    assert item.assessment.risk_analysis.unresolved_questions == ["評価10に残った問い"]
    assert item.case_reassessment_reason == "今回の根拠で当初仮説の変化を検討した。"
    if original_kind == "reference":
        assert item.original_assessment.assessment_id == original.assessment_id
        assert item.original_assessment.investment_case_explanation == "評価1の理由"
        assert item.original_assessment.core_sha256 == original.core_sha256
        assert item.original_assessment.review_id == original.review.review_id
        assert item.original_assessment.risk_analysis.unresolved_questions == ["評価1に残った問い"]
    else:
        assert item.original_assessment is None
        assert getattr(item, next(iter(origin))) == next(iter(origin.values()))
    if action == "hold":
        assert item.holding_basis == "investment_case_intact"
        assert item.return_calculation_unavailable_reason == "旧日起点で当日returnは未計算。"
    else:
        assert item.reason_code == "economic_invalidation"
        assert item.terms is None
        assert item.terms_unavailable_reasons[0].message == "価格未確認"


def test_reviewed_defer_and_unassessed_unfinished_are_distinct_and_keep_order(payload):
    pair = _pair(partial=True)
    _current(payload, pair)
    item = payload["items"][0]
    for field in ("terms", "terms_unavailable_reasons", "timing", "proposed_limit_yen"):
        item.pop(field)
    item.update(
        action="defer",
        blockers=[{"code": "uncertain", "message": "反証を確認中"}],
        resume_conditions=["反証範囲を確認して再開"],
    )
    payload["research_scope"]["targets"].append({"ticker": "5678", "purpose": "new"})
    payload["research_scope"]["outcomes"].append(
        {
            "ticker": "5678",
            "purpose": "new",
            "status": "unfinished",
            "unfinished_reason": "一次資料確認未了",
            "references": ["fixture:unfinished"],
        }
    )
    payload["items"].append(
        {
            "ticker": "5678",
            "action": "defer",
            "rationale": "未調査を見送りにしない",
            "review_conditions": ["資料確認"],
            "blockers": [{"code": "research_unfinished", "message": "調査未了"}],
            "resume_conditions": ["一次資料確認後に再開"],
        }
    )
    view = project_saved_proposal(TradeProposal.model_validate(payload), {pair.assessment_id: pair})
    first, unfinished = view.items
    assert [row.ticker for row in view.items] == ["1234", "5678"]
    assert first.assessment.stance == "uncertain"
    assert first.assessment.investment_case_status == "uncertain"
    assert first.assessment.review.primary_source_check == "partially_verified"
    assert first.assessment.review.reviewed_at == pair.review.reviewed_at
    assert first.assessment.review.review_scope == pair.review.review_scope
    assert first.assessment.review.strongest_countercase == pair.review.strongest_countercase
    assert (
        first.assessment.review.uncertainty_acceptance_reason
        == pair.review.uncertainty_acceptance_reason
    )
    assert first.unfinished is None
    assert unfinished.assessment is None
    assert unfinished.unfinished.reason == "一次資料確認未了"
    assert unfinished.unfinished.references == ["fixture:unfinished"]
    assert unfinished.blockers[0].code == "research_unfinished"
    assert unfinished.resume_conditions == ["一次資料確認後に再開"]


def test_no_action_is_a_saved_conclusion_not_defer(payload):
    pair = _pair()
    _current(payload, pair)
    item = payload["items"][0]
    for field in ("terms", "terms_unavailable_reasons", "timing", "proposed_limit_yen"):
        item.pop(field)
    item["action"] = "no_action"
    view = project_saved_proposal(TradeProposal.model_validate(payload), {pair.assessment_id: pair})
    assert view.items[0].action == "no_action"
    assert view.items[0].assessment is not None
    assert "blockers" not in view.items[0].model_dump()


def test_source_reads_exact_saved_pair_without_market_or_current_writers(tmp_path, mocker):
    case = backend_case(tmp_path)
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case))
    document, review = pair_payload(review_number=90)
    AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(90), document, review, supersedes_id=case.pair.assessment_id
    )
    case.market.unlink()
    case.runs.unlink()
    before = case.app.read_bytes()
    preview = mocker.patch(
        "baibai_engine.investment_judgment.trade_proposal.preview.preview_current_terms"
    )
    writer = mocker.patch.object(TradeProposalService, "publish")
    current_inputs = mocker.patch(
        "baibai_engine.investment_judgment.trade_proposal.inputs.collect_proposal_inputs"
    )
    original_reader = proposal_source.require_assessment_ref

    def read_exact(connection, reference):
        assert connection.in_transaction
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        return original_reader(connection, reference)

    reader = mocker.patch.object(proposal_source, "require_assessment_ref", side_effect=read_exact)
    generated_at = PUBLISHED + timedelta(days=3)
    view = build_latest_proposal(DbProposalSource(case.app), generated_at=generated_at)
    assert view.status == "ready"
    assert view.generated_at == generated_at
    assert view.proposal.proposal_id == saved.proposal_id
    assert view.proposal.items[0].assessment.assessment_id == case.pair.assessment_id
    assert view.proposal.items[0].assessment.valuation.price_yen == str(
        case.pair.document.input_snapshot.facts[0].value
    )
    assert reader.call_args.args[1] == saved.items[0].assessment_ref
    preview.assert_not_called()
    writer.assert_not_called()
    current_inputs.assert_not_called()
    assert case.app.read_bytes() == before
    assert not case.market.exists()
    assert not case.runs.exists()


def test_source_resolves_current_and_original_refs_in_the_same_transaction(
    tmp_path, payload, mocker
):
    original, current = _pair(number=1), _pair(number=10)
    _current(payload, current)
    _holding(payload, action="hold", original={"original_assessment_ref": _ref(original)})
    saved = TradeProposal.model_validate(payload)
    path = tmp_path / "application.sqlite"
    initialize_database(path)
    mocker.patch.object(proposal_source, "load_latest_investment_proposal", return_value=saved)
    pairs = {pair.assessment_id: pair for pair in (original, current)}
    connections = []

    def read_exact(connection, reference):
        assert connection.in_transaction
        connections.append(connection)
        return pairs[reference.assessment_id]

    reader = mocker.patch.object(proposal_source, "require_assessment_ref", side_effect=read_exact)
    view = DbProposalSource(path).latest()
    assert reader.call_count == 2
    assert connections[0] is connections[1]
    assert view.items[0].assessment.assessment_id == current.assessment_id
    assert view.items[0].original_assessment.assessment_id == original.assessment_id


@pytest.mark.parametrize("store", ["missing", "uninitialized", "schema", "corrupt", "empty"])
def test_empty_is_only_a_readable_formal_store_with_no_proposal(tmp_path, store):
    path = tmp_path / "application.sqlite"
    if store == "uninitialized":
        path.touch()
    elif store in {"schema", "empty"}:
        initialize_database(path)
        if store == "schema":
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("DROP TABLE investment_proposal")
    elif store == "corrupt":
        path.write_bytes(b"not a sqlite database")
    before = path.read_bytes() if path.exists() else None
    view = build_latest_proposal(DbProposalSource(path), generated_at=PUBLISHED)
    assert view.status == ("empty" if store == "empty" else "unavailable")
    assert view.proposal is None
    assert view.error == (None if store == "empty" else "保存済み提案を読み取れません。")
    assert (path.read_bytes() if path.exists() else None) == before


def test_corrupt_latest_is_unavailable_and_does_not_expose_saved_payload_or_paths(tmp_path):
    case = backend_case(tmp_path)
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case))
    with closing(sqlite3.connect(case.app)) as connection, connection:
        connection.execute("DROP TRIGGER investment_proposal_immutable")
        connection.execute(
            "UPDATE investment_proposal SET draft_sha256=? WHERE proposal_id=?",
            ("0" * 64, saved.proposal_id),
        )
    before = case.app.read_bytes()
    view = build_latest_proposal(DbProposalSource(case.app), generated_at=PUBLISHED)
    assert view.status == "unavailable"
    assert view.proposal is None
    assert view.error == "保存済み提案を読み取れません。"
    assert case.app.read_bytes() == before
