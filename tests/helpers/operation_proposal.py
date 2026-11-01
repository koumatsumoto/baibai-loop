"""Operation境界の固定提案入力。経済gateの試験はproposal helperが所有する。"""

import json
from contextlib import closing
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from baibai_engine.investment_judgment.trade_proposal.contracts import (
    TradeProposal,
    proposal_draft_sha256,
)
from baibai_engine.investment_judgment.workflow.operation.progress import (
    proposal_scope,
    selection_binding,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.write import connect_rw

PUBLISHED = datetime.fromisoformat("2026-10-14T16:20:00+09:00")


def proposal_for(operation, *, actions=None, number=1):
    """Operation境界を試験する固定提案。経済gateはProposalServiceの試験が所有する。"""
    fixture = Path(__file__).parents[1] / "fixtures/domain-assessment-proposal-synthetic.json"
    raw = deepcopy(json.loads(fixture.read_text())["investment_proposal"])
    selected = selection_binding(operation.payload)
    scope = proposal_scope(operation.operation_id, operation.payload)
    raw.update(
        proposal_id=f"investment-proposal-00000000-0000-4000-8000-{number:012x}",
        request_scope=selected.request.scope,
        research_scope=scope.model_dump(mode="json"),
        published_at=PUBLISHED.isoformat(),
        liquidity_context=[],
    )
    items, positions = [], []
    template = deepcopy(raw["items"][0])
    for outcome in scope.outcomes:
        if outcome.status == "unfinished":
            items.append(
                {
                    "ticker": outcome.ticker,
                    "action": "defer",
                    "rationale": "未調査を経済的見送りへ混ぜない",
                    "review_conditions": ["一次資料の到着時に再評価する"],
                    "assessment_ref": None,
                    "blockers": [
                        {"code": "research_unfinished", "message": outcome.unfinished_reason}
                    ],
                    "resume_conditions": ["必要な一次資料を確認する"],
                }
            )
            continue
        action = (actions or {}).get(outcome.ticker, "no_action")
        item = {
            "ticker": outcome.ticker,
            "action": action,
            "rationale": "合成の比較理由",
            "review_conditions": ["新しい反証を確認したら再評価する"],
            "assessment_ref": outcome.assessment_ref.model_dump(mode="json"),
        }
        if action in {"hold", "sell"}:
            position = {
                "ticker": outcome.ticker,
                "sector": "未分類",
                "common_factors": [],
                "quantity": 200,
                "cost_yen": "240000",
                "quantity_basis_confirmed": True,
                "original_reason_unavailable_reason": "旧当初理由は確認できない",
            }
            positions.append(position)
            item.update(
                position=position, case_reassessment_reason="今回仮説の維持/無効化を確認した"
            )
            if action == "hold":
                item.update(
                    holding_basis="investment_case_intact",
                    remaining_reward=None,
                    return_calculation_unavailable_reason="旧日起点につき同日収益算術は未確定",
                )
            else:
                item.update(
                    reason_code="economic_invalidation",
                    remaining_reward=None,
                    terms=None,
                    terms_unavailable_reasons=[
                        {"code": "quote_missing_or_stale", "message": "現在価格が未確認"}
                    ],
                )
        elif action == "buy":
            item.update(timing="wait_limit", terms=template["terms"], terms_unavailable_reasons=[])
        elif action == "buy_needs_terms":
            item.update(
                action="buy",
                timing="wait_limit",
                terms=None,
                proposed_limit_yen=template["terms"]["limit_price_yen"],
                terms_unavailable_reasons=[
                    {"code": "price_grid_rule_unavailable", "message": "対象市場分類が未確認"}
                ],
            )
        items.append(item)
    raw["items"] = items
    raw["inputs"]["positions"] = positions
    raw["review"]["review_id"] = f"proposal-review-00000000-0000-4000-8000-{number:012x}"
    value = TradeProposal.model_validate(raw)
    return value.model_copy(
        update={
            "review": value.review.model_copy(update={"draft_sha256": proposal_draft_sha256(value)})
        }
    )


def seed_proposal(db, proposal):
    with closing(connect_rw(db)) as connection, connection:
        connection.execute(
            "INSERT INTO investment_proposal VALUES (?,?,?,?,?,?,?)",
            (
                proposal.proposal_id,
                proposal.as_of.isoformat(),
                proposal.prepared_at.isoformat(),
                proposal.published_at.isoformat(),
                proposal.request_scope,
                canonical_json(proposal.model_dump(mode="json")),
                proposal_draft_sha256(proposal),
            ),
        )
        connection.executemany(
            "INSERT INTO investment_proposal_item VALUES (?,?,?,?,?)",
            [
                (
                    proposal.proposal_id,
                    index,
                    item.ticker,
                    item.action,
                    None if item.assessment_ref is None else item.assessment_ref.assessment_id,
                )
                for index, item in enumerate(proposal.items)
            ],
        )
        for item in proposal.items:
            if item.action == "buy" and item.terms is not None:
                connection.execute(
                    "INSERT INTO position_decision_reference VALUES (?,?,?,?,?,?,?)",
                    (
                        proposal.proposal_id,
                        item.ticker,
                        "buy",
                        "proposal",
                        proposal.proposal_id,
                        proposal.proposal_id,
                        proposal_draft_sha256(proposal),
                    ),
                )
