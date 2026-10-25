"""正式選定から評価pair、保存提案、完成/未完成への進行を合成正本で確認する。"""

import json
import socket
import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.helpers.assessment_v1 import NOW, assessment_id, pair_payload
from tests.helpers.db_seed import seed_ledger
from tests.helpers.generated_selection import seed_generated_selection
from tests.helpers.ledger import load_portfolio_ledger
from tests.helpers.research_selection import selection as make_selection
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.foundation.json import canonical_json
from baibai_engine.investment_judgment.inputs import project_research_inputs
from baibai_engine.investment_judgment.operation.models import OperationPayload
from baibai_engine.investment_judgment.operation.progress import (
    ResearchProgress,
    progress_binding,
    proposal_scope,
    selection_binding,
    with_progress,
)
from baibai_engine.investment_judgment.operation.research_binding import (
    require_proposal_research_scope,
)
from baibai_engine.investment_judgment.operation.service import (
    OperationCompletionError,
    OperationConflictError,
    OperationService,
)
from baibai_engine.investment_judgment.stock_assessment_store import publish_reviewed_assessment
from baibai_engine.investment_judgment.trade_proposal import TradeProposal, proposal_draft_sha256
from baibai_engine.investment_judgment.trade_proposal_query import load_investment_proposal
from baibai_engine.storage.application.read import connect_read_only
from baibai_engine.storage.application.write import connect_rw, initialize_database

PUBLISHED = datetime.fromisoformat("2026-10-14T16:20:00+09:00")


@pytest.fixture
def case(tmp_path):
    db, market = tmp_path / "app.sqlite", tmp_path / "absent-market.sqlite"
    initialize_database(db)
    return db, market


def ledger_rows(db):
    with closing(sqlite3.connect(db)) as connection, connection:
        return connection.execute("SELECT payload FROM ledger_event ORDER BY append_seq").fetchall()


def seed_assessment(db, ticker="1234", *, number=1, now=NOW):
    document, review = pair_payload(ticker=ticker, review_number=number + 100)
    with closing(connect_rw(db)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        publish_reviewed_assessment(
            connection, assessment_id(number), document, review, published_at=now
        )
    return assessment_id(number)


def start(case, tickers=(), *, scope="discover_new"):
    db, _market = case
    runs = db.with_name("runs.sqlite")
    value = seed_generated_selection(runs=runs, now=NOW, tickers=tickers, scope=scope)
    service = OperationService(db, runs_path=runs)
    operation = service.start(
        session_kind="position-review" if scope == "review_positions" else "capital-allocation",
        as_of=NOW.date(),
        started_at=NOW,
        ticker=tickers[0] if scope == "review_positions" and len(tickers) == 1 else None,
        payload=OperationPayload(
            checkpoint="合成依頼を明示開始", artifacts=(value.model_dump(mode="json"),)
        ),
    )
    return service, operation


def final_payload(operation):
    return operation.payload.model_copy(
        update={
            "result": "対象を調査し提案を保存、または0対象の理由を明示",
            "next": "人間の裁定または次の明示依頼",
        }
    )


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


def record_and_finish(service, operation, proposal):
    recorded = service.record_proposal(operation.operation_id, proposal.proposal_id, now=PUBLISHED)
    return service.complete(operation.operation_id, final_payload(recorded), completed_at=PUBLISHED)


def test_empty_selection_ends_with_reason_without_fabricating_proposal_or_confirmation(case):
    db, _ = case
    before = ledger_rows(db)
    service, operation = start(case)
    completed = service.complete(operation.operation_id, final_payload(operation), completed_at=NOW)
    assert completed.status == "completed"
    assert "human_confirmation" not in completed.payload.model_dump()
    assert ledger_rows(db) == before
    with closing(sqlite3.connect(db)) as connection, connection:
        assert connection.execute("SELECT count(*) FROM investment_proposal").fetchone()[0] == 0


def test_reviewed_a_and_unfinished_b_keep_complete_scope_and_distinct_actions(case):
    db, _ = case
    reference = seed_assessment(db)
    service, operation = start(case, ("1234", "5678"))
    service.record_assessment(operation.operation_id, reference, now=NOW)
    with pytest.raises(ValueError, match="covered"):
        proposal_for(service.get(operation.operation_id))
    service.record_unfinished(
        operation.operation_id,
        ticker="5678",
        reason="必要な一次資料を確認できない",
        references=("https://example.invalid/synthetic-disclosure",),
        now=NOW,
    )
    operation = service.get(operation.operation_id)
    proposal = proposal_for(operation)
    assert [item.action for item in proposal.items] == ["no_action", "defer"]
    assert [row.status for row in proposal.research_scope.outcomes] == ["reviewed", "unfinished"]
    seed_proposal(db, proposal)
    before = ledger_rows(db)
    completed = record_and_finish(service, operation, proposal)
    assert "human_confirmation" not in completed.payload.model_dump()
    assert (
        progress_binding(completed.payload).proposal_ref.draft_sha256
        == proposal.review.draft_sha256
    )
    assert ledger_rows(db) == before
    with pytest.raises(OperationConflictError, match="immutable"):
        service.record_assessment(operation.operation_id, reference, now=PUBLISHED)


def test_all_unfinished_requires_saved_defer_proposal_and_does_not_become_rejection(case):
    db, _ = case
    service, operation = start(case, ("1234",))
    operation = service.record_unfinished(
        operation.operation_id,
        ticker="1234",
        reason="研究不足",
        references=("synthetic-source",),
        now=NOW,
    )
    with pytest.raises(OperationCompletionError, match="published Trade Proposal"):
        service.complete(operation.operation_id, final_payload(operation), completed_at=PUBLISHED)
    proposal = proposal_for(operation)
    seed_proposal(db, proposal)
    completed = record_and_finish(service, operation, proposal)
    assert progress_binding(completed.payload).outcomes[0].status == "unfinished"
    assert proposal.items[0].action == "defer"
    assert proposal.items[0].assessment_ref is None


@pytest.mark.parametrize("action", ["hold", "sell"])
def test_holding_uses_common_assessment_reference_and_published_proposal_only(case, action):
    db, _ = case
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    seed_ledger(db, ledger)
    reference = seed_assessment(db, "2331")
    service, operation = start(case, ("2331",), scope="review_positions")
    operation = service.record_assessment(operation.operation_id, reference, now=NOW)
    before = ledger_rows(db)
    progress = progress_binding(operation.payload)
    assert progress.outcomes[0].purpose == "holding"
    assert progress.outcomes[0].assessment_ref.assessment_id == reference
    assert "draft" not in progress.model_dump_json()
    with pytest.raises(OperationCompletionError, match="published Trade Proposal"):
        service.complete(operation.operation_id, final_payload(operation), completed_at=PUBLISHED)
    proposal = proposal_for(operation, actions={"2331": action})
    seed_proposal(db, proposal)
    completed = record_and_finish(service, operation, proposal)
    assert completed.status == "completed"
    assert ledger_rows(db) == before
    assert proposal.items[0].position.original_assessment_ref is None
    assert proposal.items[0].position.original_reason_unavailable_reason


def test_selection_immutable_and_missing_outcomes_cannot_drop_targets(case):
    db, _ = case
    service, operation = start(case, ("1234", "5678"))
    with pytest.raises(OperationCompletionError, match="covered"):
        service.complete(operation.operation_id, final_payload(operation), completed_at=PUBLISHED)
    selected = selection_binding(operation.payload)
    changed = selected.model_copy(update={"selection_summary": "別の選定"})
    with pytest.raises(OperationConflictError, match="immutable"):
        service.checkpoint(
            operation.operation_id,
            operation.payload.model_copy(update={"artifacts": (changed.model_dump(mode="json"),)}),
        )
    assert service.get(operation.operation_id) == operation
    assert ledger_rows(db) == []


@pytest.mark.parametrize("change", ["hash", "review", "ticker", "purpose"])
def test_exact_assessment_pair_and_selected_purpose_are_verified(case, change):
    db, _ = case
    reference = seed_assessment(db)
    service, operation = start(case, ("1234",))
    operation = service.record_assessment(operation.operation_id, reference, now=NOW)
    progress = progress_binding(operation.payload)
    outcome = progress.outcomes[0]
    if change in {"hash", "review"}:
        field, value = (
            ("core_sha256", "0" * 64)
            if change == "hash"
            else ("review_id", "assessment-review-00000000-0000-4000-8000-000000000099")
        )
        outcome = outcome.model_copy(
            update={"assessment_ref": outcome.assessment_ref.model_copy(update={field: value})}
        )
    else:
        outcome = outcome.model_copy(update={change: "5678" if change == "ticker" else "holding"})
    with pytest.raises(ValueError, match=r"differs|unselected"):
        service.checkpoint(
            operation.operation_id,
            with_progress(operation.payload, progress.model_copy(update={"outcomes": (outcome,)})),
            now=NOW,
        )
    assert service.get(operation.operation_id) == operation


def test_future_assessment_publication_cannot_be_recorded(case):
    db, _ = case
    reference = seed_assessment(db, now=NOW + timedelta(minutes=1))
    service, operation = start(case, ("1234",))
    with pytest.raises(ValueError, match="precedes Assessment publication"):
        service.record_assessment(operation.operation_id, reference, now=NOW)
    assert service.get(operation.operation_id) == operation


def test_retry_is_stable_and_checkpoint_cannot_remove_recorded_outcomes_or_reference(case):
    db, _ = case
    reference = seed_assessment(db)
    service, operation = start(case, ("1234",))
    operation = service.record_assessment(operation.operation_id, reference, now=NOW)
    assert service.record_assessment(operation.operation_id, reference, now=PUBLISHED) == operation
    proposal = proposal_for(operation)
    seed_proposal(db, proposal)
    operation = service.record_proposal(operation.operation_id, proposal.proposal_id, now=PUBLISHED)
    assert (
        service.record_proposal(
            operation.operation_id, proposal.proposal_id, now=PUBLISHED + timedelta(days=1)
        )
        == operation
    )
    for update in ({"outcomes": ()}, {"proposal_ref": None}):
        progress = progress_binding(operation.payload).model_copy(update=update)
        with pytest.raises(ValueError, match="silently removed"):
            service.checkpoint(
                operation.operation_id, with_progress(operation.payload, progress), now=PUBLISHED
            )
        assert service.get(operation.operation_id) == operation


def test_late_outcome_preserves_saved_proposal_but_completion_requires_current_scope(case):
    db, _ = case
    first, later = seed_assessment(db), seed_assessment(db, "5678", number=2)
    service, operation = start(case, ("1234", "5678"))
    service.record_assessment(operation.operation_id, first, now=NOW)
    operation = service.record_unfinished(
        operation.operation_id,
        ticker="5678",
        reason="未確認資料",
        references=("synthetic",),
        now=NOW,
    )
    proposal = proposal_for(operation)
    seed_proposal(db, proposal)
    service.record_proposal(operation.operation_id, proposal.proposal_id, now=PUBLISHED)
    operation = service.record_assessment(operation.operation_id, later, now=PUBLISHED)
    with closing(connect_read_only(db)) as reader:
        assert load_investment_proposal(reader, proposal.proposal_id) == proposal
        with pytest.raises(ValueError, match="scope differs"):
            require_proposal_research_scope(
                reader,
                scope=proposal.research_scope,
                request_scope=proposal.request_scope,
                published_at=PUBLISHED,
            )
    with pytest.raises(OperationCompletionError, match="scope differs"):
        service.complete(operation.operation_id, final_payload(operation), completed_at=PUBLISHED)
    replacement = proposal_for(operation, number=2)
    seed_proposal(db, replacement)
    assert record_and_finish(service, operation, replacement).status == "completed"
    with closing(connect_read_only(db)) as reader:
        assert load_investment_proposal(reader, proposal.proposal_id) == proposal


@pytest.mark.parametrize(
    "mutation", ["selection_hash", "reason", "references", "request", "unknown"]
)
def test_publication_requires_exact_current_selection_and_outcomes(case, mutation):
    db, _ = case
    service, operation = start(case, ("1234",))
    operation = service.record_unfinished(
        operation.operation_id, ticker="1234", reason="未調査", references=("synthetic",), now=NOW
    )
    scope = proposal_scope(operation.operation_id, operation.payload)
    request_scope = "discover_new"
    with closing(connect_read_only(db)) as reader:
        assert require_proposal_research_scope(
            reader, scope=scope, request_scope=request_scope, published_at=PUBLISHED
        ) == selection_binding(operation.payload)
        if mutation == "selection_hash":
            scope = scope.model_copy(update={"selection_sha256": "0" * 64})
        elif mutation in {"reason", "references"}:
            field, value = (
                ("unfinished_reason", "別の理由")
                if mutation == "reason"
                else ("references", ("other-source",))
            )
            scope = scope.model_copy(
                update={"outcomes": (scope.outcomes[0].model_copy(update={field: value}),)}
            )
        elif mutation == "request":
            request_scope = "specified_stocks"
        else:
            scope = scope.model_copy(update={"operation_id": "research-op-" + "f" * 32})
        with pytest.raises(ValueError, match=r"scope differs|requires its current"):
            require_proposal_research_scope(
                reader, scope=scope, request_scope=request_scope, published_at=PUBLISHED
            )


@pytest.mark.parametrize("action", ["buy", "buy_needs_terms"])
def test_only_concrete_buy_terms_complete_purchase_proposal(case, action):
    db, _ = case
    reference = seed_assessment(db)
    service, operation = start(case, ("1234",))
    operation = service.record_assessment(operation.operation_id, reference, now=NOW)
    proposal = proposal_for(operation, actions={"1234": action})
    seed_proposal(db, proposal)
    operation = service.record_proposal(operation.operation_id, proposal.proposal_id, now=PUBLISHED)
    if action == "buy_needs_terms":
        with pytest.raises(OperationCompletionError, match="concrete order terms"):
            service.complete(
                operation.operation_id, final_payload(operation), completed_at=PUBLISHED
            )
        assert service.get(operation.operation_id) == operation
    else:
        # Completion a day later only verifies the stored publication/terms.
        # Current price/cash/target session belong to preview and do not rewrite terms.
        completed = service.complete(
            operation.operation_id,
            final_payload(operation),
            completed_at=PUBLISHED + timedelta(days=1),
        )
        assert completed.status == "completed"
        with closing(connect_read_only(db)) as reader:
            assert load_investment_proposal(reader, proposal.proposal_id) == proposal


def test_proposal_hash_future_publication_and_completed_publish_are_checked(case):
    db, _ = case
    reference = seed_assessment(db)
    service, operation = start(case, ("1234",))
    operation = service.record_assessment(operation.operation_id, reference, now=NOW)
    proposal = proposal_for(operation)
    seed_proposal(db, proposal)
    with pytest.raises(OperationCompletionError, match="precedes Proposal publication"):
        service.record_proposal(operation.operation_id, proposal.proposal_id, now=NOW)
    assert service.get(operation.operation_id) == operation
    operation = service.record_proposal(operation.operation_id, proposal.proposal_id, now=PUBLISHED)
    progress = progress_binding(operation.payload)
    progress = progress.model_copy(
        update={"proposal_ref": progress.proposal_ref.model_copy(update={"draft_sha256": "0" * 64})}
    )
    with pytest.raises(OperationCompletionError, match="saved draft hash"):
        service.complete(
            operation.operation_id,
            final_payload(operation).model_copy(
                update={"artifacts": with_progress(operation.payload, progress).artifacts}
            ),
            completed_at=PUBLISHED,
        )
    record_and_finish(service, operation, proposal)
    with (
        closing(connect_read_only(db)) as reader,
        pytest.raises(ValueError, match="active Research Operation"),
    ):
        require_proposal_research_scope(
            reader,
            scope=proposal.research_scope,
            request_scope=proposal.request_scope,
            published_at=PUBLISHED,
        )


def test_new_research_progress_rejects_inline_old_holding_proposal_and_blank_reason():
    with pytest.raises(ValidationError):
        ResearchProgress.model_validate(
            {
                "outcomes": [
                    {"status": "proposal", "ticker": "1234", "purpose": "holding", "draft": {}}
                ]
            }
        )
    with pytest.raises(ValidationError):
        ResearchProgress.model_validate(
            {
                "outcomes": [
                    {
                        "status": "unfinished",
                        "ticker": "1234",
                        "purpose": "new",
                        "unfinished_reason": " ",
                        "references": ["synthetic"],
                    }
                ]
            }
        )


def test_domain_inputs_preserve_gaps_and_null_and_do_not_write_or_use_network(
    case, tmp_path, monkeypatch
):
    db, market = case
    before = db.read_bytes()

    def no_network(*args, **kwargs):
        pytest.fail("research inputs attempted network I/O")

    monkeypatch.setattr(socket, "socket", no_network)
    selected = make_selection(now=NOW, tickers=("1234",))
    inputs = selected.inputs
    projected = project_research_inputs(selected.model_copy(update={"inputs": inputs}))
    assert set(projected["securities"]) == {"1234"}
    assert projected["known_at"] == selected.input_known_at.isoformat()
    assert "received_cutoff" not in projected
    assert projected["market_context"]["saved_market_context_assessment"] is None
    assert projected["market_context"]["reading"] is None
    assert (
        projected["securities"]["1234"]["machine_analysis_unavailable_reason"]
        == selected.inputs.input_gaps[0].reason
    )
    assert projected["securities"]["1234"]["machine_valuation"] is None
    assert db.read_bytes() == before
    assert not market.exists()
    assert not (tmp_path / "absent-indicators.sqlite").exists()


def test_bollinger_context_is_frozen_in_research_inputs(case, tmp_path, monkeypatch):
    _db, market = case
    insert_daily_bars_from_closes(market, "1234", [100] * 19 + [120], end_date=NOW.date())
    selected = make_selection(now=NOW, tickers=("1234",))
    from baibai_engine.machine_analysis.technical_analysis.query import technical_analysis

    context = dict(selected.inputs.securities_context["1234"])
    context["technical_analysis"] = technical_analysis(market, ticker="1234", as_of=NOW.date())
    inputs = selected.inputs.model_copy(update={"securities_context": {"1234": context}})
    saved = type(selected).model_validate_json(
        selected.model_copy(update={"inputs": inputs}).model_dump_json()
    )
    expected = deepcopy(inputs.securities_context["1234"]["technical_analysis"])
    assert expected["bollinger_20bar"]["position"] == "above_upper"
    assert expected["bollinger_20bar"]["z_score"] == pytest.approx(19**0.5)
    assert expected["rsi_14bar"]["value"] == 100
    assert expected["moving_averages"]["sma_200bar"]["status"] == "insufficient_history"
    with closing(sqlite3.connect(market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=90")

    def no_recompute(*args, **kwargs):
        pytest.fail("projecting saved research inputs must not recalculate prices")

    monkeypatch.setattr(
        "baibai_engine.machine_analysis.technical_analysis.query.technical_analysis", no_recompute
    )
    projected = project_research_inputs(saved)
    assert projected["securities"]["1234"]["technical_analysis"] == expected
    historical = saved.model_dump(mode="json")
    del historical["inputs"]["securities_context"]["1234"]["technical_analysis"]
    legacy_bb = {
        key: expected["bollinger_20bar"][key]
        for key in (
            "status",
            "window_bars",
            "window_start_date",
            "window_end_date",
            "z_score",
            "position",
        )
    }
    historical["inputs"]["securities_context"]["1234"]["market_context"] = {
        "price": {"bollinger_20bar": legacy_bb}
    }
    old_projection = project_research_inputs(type(selected).model_validate(historical))
    assert "technical_analysis" not in old_projection["securities"]["1234"]
    assert (
        old_projection["securities"]["1234"]["market_context"]["price"]["bollinger_20bar"]
        == legacy_bb
    )


def test_equivalent_publication_instants_use_aware_time_comparison(case):
    db, _ = case
    reference = seed_assessment(db)
    service, operation = start(case, ("1234",))
    operation = service.record_assessment(
        operation.operation_id, reference, now=NOW.astimezone(UTC)
    )
    proposal = proposal_for(operation)
    seed_proposal(db, proposal)
    operation = service.record_proposal(
        operation.operation_id, proposal.proposal_id, now=PUBLISHED.astimezone(UTC)
    )
    assert (
        service.complete(
            operation.operation_id, final_payload(operation), completed_at=PUBLISHED.astimezone(UTC)
        ).status
        == "completed"
    )


@pytest.mark.parametrize("field", ["result", "next"])
def test_zero_targets_still_need_a_nonblank_reason_and_next_step(case, field):
    service, operation = start(case)
    payload = final_payload(operation).model_copy(update={field: " "})
    with pytest.raises(OperationCompletionError, match=field):
        service.complete(operation.operation_id, payload, completed_at=NOW)
    assert service.get(operation.operation_id) == operation


def test_selected_ticker_cannot_borrow_another_tickers_assessment_pair(case):
    db, _ = case
    reference = seed_assessment(db)
    service, operation = start(case, ("1234", "5678"))
    operation = service.record_assessment(operation.operation_id, reference, now=NOW)
    progress = progress_binding(operation.payload)
    borrowed = progress.outcomes[0].model_copy(update={"ticker": "5678"})
    with pytest.raises(ValueError, match="Reviewed Assessment ticker"):
        service.checkpoint(
            operation.operation_id,
            with_progress(
                operation.payload,
                progress.model_copy(update={"outcomes": (*progress.outcomes, borrowed)}),
            ),
            now=NOW,
        )
    assert service.get(operation.operation_id) == operation


def test_a_new_operation_cannot_bind_the_previous_operations_proposal(case):
    db, _ = case
    reference = seed_assessment(db)
    service, operation = start(case, ("1234",))
    operation = service.record_assessment(operation.operation_id, reference, now=NOW)
    proposal = proposal_for(operation)
    seed_proposal(db, proposal)
    completed = record_and_finish(service, operation, proposal)
    service, other = start(case, ("1234",))
    with pytest.raises(OperationConflictError, match="another Research Operation"):
        service.record_proposal(other.operation_id, proposal.proposal_id, now=PUBLISHED)
    assert service.get(other.operation_id) == other
    assert service.get(operation.operation_id) == completed


def test_stale_progress_write_cannot_overwrite_a_concurrent_outcome(case):
    db, _ = case
    reference = seed_assessment(db)
    service, operation = start(case, ("1234", "5678"))
    stale_payload = operation.payload
    updated = service.record_assessment(operation.operation_id, reference, now=NOW)
    with pytest.raises(OperationConflictError, match="progress changed"):
        service._replace_active(
            operation.operation_id,
            payload=stale_payload,
            completed_at=None,
            checked_at=NOW,
            expected_payload=stale_payload,
        )
    assert service.get(operation.operation_id) == updated


def test_naive_progress_instant_is_rejected_without_recording_an_assessment(case):
    db, _ = case
    reference = seed_assessment(db)
    service, operation = start(case, ("1234",))
    with pytest.raises(ValueError, match="UTC offset"):
        service.record_assessment(operation.operation_id, reference, now=NOW.replace(tzinfo=None))
    assert service.get(operation.operation_id) == operation
