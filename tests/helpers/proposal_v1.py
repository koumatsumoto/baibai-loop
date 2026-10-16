"""正式writerと現行schemaで作る投資提案の合成fixture。"""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from baibai_engine.position.ledger import PortfolioLedgerDocument
from baibai_engine.research.assessment import StockAssessmentDocument
from baibai_engine.research.assessment_store import AssessmentStoreService
from baibai_engine.research.operation.models import OperationPayload
from baibai_engine.research.operation.service import OperationService
from baibai_engine.research.proposal import InvestmentProposal, proposal_draft_sha256
from baibai_engine.research.proposal_scaffold import scaffold_proposal
from baibai_engine.research.proposal_warnings import build_proposal_warnings
from baibai_engine.storage.application.write import initialize_database
from tests.helpers.assessment_v1 import (
    assessment_id,
    pair_payload,
    update_review_hash,
)
from tests.helpers.db_seed import seed_ledger
from tests.helpers.research_selection import selection
from tests.helpers.screening_sqlite import seed_daily_bars

PREPARED = datetime.fromisoformat("2026-10-14T16:10:00+09:00")
PUBLISHED = datetime.fromisoformat("2026-10-14T16:20:00+09:00")
PROPOSAL_ID = "investment-proposal-00000000-0000-4000-8000-000000000003"


def backend_case(
    tmp_path: Path,
    *,
    holding=False,
    active_sell=False,
    broken=False,
    cash_yen=300000,
    assessment_as_of="2026-10-14",
    valuation_unresolved=False,
    tickers=("1234",),
):
    """実writerで固定選定/Operationと評価・Reviewのpairを作る。新Operation行のSQL seedはしない。"""
    app, market = tmp_path / "app.sqlite", tmp_path / "market.sqlite"
    initialize_database(app)
    events = [
        {
            "type": "opening_balance",
            "event_id": "synthetic-opening",
            "occurred_at": "2026-10-14T09:00:00+09:00",
            "amount_yen": cash_yen,
        }
    ]
    if holding:
        events.extend(
            [
                {
                    "type": "reservation",
                    "event_id": "synthetic-initial-order",
                    "reservation_id": "synthetic-initial-reservation",
                    "order_id": "synthetic-initial-broker-order",
                    "occurred_at": "2026-10-14T09:01:00+09:00",
                    "ticker": "1234",
                    "side": "buy",
                    "sector": "情報・通信業",
                    "common_factors": ["domestic-demand"],
                    "decision_reference": "synthetic-retained-reference",
                    "quantity": 100,
                    "price_guard_yen": "900",
                    "expires_at": "2026-10-14T15:30:00+09:00",
                },
                {
                    "type": "execution",
                    "event_id": "synthetic-initial-fill",
                    "execution_id": "synthetic-initial-execution",
                    "occurred_at": "2026-10-14T09:02:00+09:00",
                    "ticker": "1234",
                    "side": "buy",
                    "reservation_id": "synthetic-initial-reservation",
                    "decision_reference": "synthetic-retained-reference",
                    "quantity": 100,
                    "price_yen": "900",
                },
            ]
        )
        if active_sell:
            events.append(
                {
                    "type": "reservation",
                    "event_id": "synthetic-sell-order",
                    "reservation_id": "synthetic-sell-reservation",
                    "order_id": "synthetic-sell-broker-order",
                    "occurred_at": "2026-10-14T09:03:00+09:00",
                    "ticker": "1234",
                    "side": "sell",
                    "sector": "情報・通信業",
                    "common_factors": ["domestic-demand"],
                    "decision_reference": None,
                    "quantity": 100,
                    "price_guard_yen": "800",
                    "expires_at": "2026-10-15T15:30:00+09:00",
                }
            )
    ledger = PortfolioLedgerDocument.model_validate(
        {
            "schema_version": 3,
            "portfolio_scope": "repository_only",
            "as_of": "2026-10-14T15:00:00+09:00",
            "events": events,
        }
    )
    seed_ledger(app, ledger)
    seed_daily_bars(market, [(ticker, "2026-10-14", 1100.0, 1.0) for ticker in tickers])
    with closing(sqlite3.connect(market)) as connection, connection:
        connection.executemany(
            "INSERT INTO jquants_master_snapshots(snapshot_date,ticker,name,market,sector_33,is_common_stock) VALUES (?,?,?,?,?,?)",
            [("2026-10-14", ticker, "合成企業", "Prime", "情報・通信業", 1) for ticker in tickers],
        )
        connection.executemany(
            "INSERT INTO jquants_market_calendar(day,holiday_division) VALUES (?,?)",
            [
                (
                    (PREPARED.date() + timedelta(days=offset)).isoformat(),
                    int((PREPARED.date() + timedelta(days=offset)).weekday() < 5),
                )
                for offset in range(16)
            ],
        )
    document, review = pair_payload(as_of=assessment_as_of)
    if broken:
        document["investment_case"]["status"] = "broken"
        document["investment_case"]["status_reason"] = "一次資料で恒常的な収益源の消失を確認。"
        document["conclusion"]["stance"] = "unsupported"
        update_review_hash(document, review)
    if valuation_unresolved:
        document["valuation"] = dict(
            status="unresolved",
            market_price_fact_id=None,
            horizon_months=None,
            required_annual_return_pct=None,
            base=None,
            downside=None,
            unresolved_reason="合成借換条件を未確認のため将来価値を確定できない。",
        )
        document["conclusion"]["stance"] = "uncertain"
        review["recalculated_projections"] = []
        update_review_hash(document, review)
    pair = AssessmentStoreService(
        app, clock=lambda: datetime.fromisoformat(f"{assessment_as_of}T16:06:00+09:00")
    ).publish_reviewed_assessment(assessment_id(), document, review)
    selected = selection(
        now=datetime.fromisoformat("2026-10-14T16:07:00+09:00"),
        tickers=tickers,
        scope="review_positions" if holding else "specified_stocks",
    )
    operations = OperationService(app)
    operation = operations.start(
        session_kind="position-review" if holding else "capital-allocation",
        as_of=selected.research_as_of,
        started_at=selected.input_known_at,
        ticker="1234" if holding and len(tickers) == 1 else None,
        payload=OperationPayload(
            checkpoint="research", artifacts=(selected.model_dump(mode="json"),)
        ),
    )
    operation = operations.record_assessment(
        operation.operation_id, pair.assessment_id, now=PREPARED
    )
    return SimpleNamespace(
        app=app,
        market=market,
        ledger=ledger,
        pair=pair,
        selected=selected,
        operations=operations,
        operation=operation,
    )


def reviewed_proposal(case, *, action="buy", proposal_id=PROPOSAL_ID):
    raw = scaffold_proposal(
        operation_id=case.operation.operation_id,
        proposal_id=proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PREPARED,
    )
    raw["headline"] = "合成の具体的提案"
    raw["comparison"] = "保存評価を現金維持と比較した。"
    raw["forgone"] = "現在1100円での買付は見送る。"
    item = raw["items"][0]
    item["rationale"] = "合成資料に基づく経済判断。"
    item["action"] = action
    if action != "defer":
        item.pop("blockers")
        item.pop("resume_conditions")
    if action == "buy":
        item.update(
            timing="wait_limit",
            terms={
                "side": "buy",
                "limit_price_yen": "1000",
                "quantity": 300,
                "notional_yen": "300000",
                "target_session": "2026-10-15",
                "expires_at": "2026-10-15T15:30:00+09:00",
                "valuation_as_of": "2026-10-14",
                "maximum_entry_price_yen": "1000",
                "price_grid_rule_id": "tse-common-conservative-20261014",
                "conditions": [
                    "仮説・株式単位・資金・注文状態に変化がないこと。実注文前に現在previewを確認する。"
                ],
                "warnings": [],
            },
        )
    elif action == "hold":
        item.update(
            position=raw["inputs"]["positions"][0],
            holding_basis="investment_case_intact",
            case_reassessment_reason="今回確認した重要な反証は無い。",
        )
    elif action == "sell":
        item.update(
            position=raw["inputs"]["positions"][0],
            reason_code="economic_invalidation",
            case_reassessment_reason="今回も恒常的な収益源の消失を確認した。",
            remaining_reward=None,
            terms=None,
            terms_unavailable_reasons=[
                {"code": "quote_missing_or_stale", "message": "現在quote無し"},
                {"code": "quantity_basis_unconfirmed", "message": "数量basisを確認できない"},
            ],
        )
    raw["published_at"] = PUBLISHED.isoformat()
    raw["review"].update(
        reviewer_identity="synthetic-independent-reviewer", reviewed_at="2026-10-14T16:15:00+09:00"
    )
    return bind_review(raw, {case.pair.assessment_id: case.pair.document})


def bind_review(raw, assessments: dict[str, StockAssessmentDocument]):
    proposal = InvestmentProposal.model_validate(raw)
    warnings = build_proposal_warnings(
        proposal.inputs, proposal.items, proposal.liquidity_context, assessments=assessments
    )
    raw["warnings"] = list(warnings.warnings)
    raw["portfolio_exposure"] = warnings.portfolio_exposure.model_dump(mode="json")
    for item in raw["items"]:
        if item["action"] in {"buy", "sell"} and item["terms"] is not None:
            item["terms"]["warnings"] = list(warnings.terms_warnings.get(item["ticker"], ()))
    proposal = InvestmentProposal.model_validate(raw)
    raw["review"]["draft_sha256"] = proposal_draft_sha256(proposal)
    return InvestmentProposal.model_validate(raw)
