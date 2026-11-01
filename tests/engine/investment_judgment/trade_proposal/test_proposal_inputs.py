from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import timedelta
from decimal import Decimal

import pytest
from tests.helpers.proposal import (
    PREPARED,
    PROPOSAL_ID,
    PUBLISHED,
    backend_case,
    bind_review,
    reviewed_proposal,
)

from baibai_engine.investment_judgment.trade_proposal.contracts import ProposalError
from baibai_engine.investment_judgment.trade_proposal.fingerprint import proposal_inputs_fingerprint
from baibai_engine.investment_judgment.trade_proposal.inputs import (
    build_proposal_context,
    collect_proposal_inputs,
)
from baibai_engine.investment_judgment.trade_proposal.query import get_investment_proposal
from baibai_engine.investment_judgment.trade_proposal.scaffold import scaffold_proposal
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.storage.sqlite.application.read import connect_read_only


def _collect(case, *, now=PREPARED, proposal_id=PROPOSAL_ID, tickers=("1234",)):
    with closing(connect_read_only(case.app)) as connection, connection:
        connection.execute("BEGIN")
        return collect_proposal_inputs(
            connection,
            tickers=tickers,
            proposal_id=proposal_id,
            now=now,
            sqlite_path=case.market,
            budget_min_yen=200000,
            budget_max_yen=300000,
        )


def test_collect_preserves_meta_cutoff_and_stable_daily_label(tmp_path):
    case = backend_case(tmp_path)
    first = _collect(case)
    second = _collect(case, now=PUBLISHED)
    assert first.checked_at != second.checked_at
    assert first.ledger_as_of == second.ledger_as_of == case.ledger.as_of
    assert first.ledger_head == second.ledger_head == 1
    assert first.quotes[0].price_as_of == second.quotes[0].price_as_of == PREPARED.date()
    assert "observed_at" not in first.quotes[0].model_dump()
    assert proposal_inputs_fingerprint(first) == proposal_inputs_fingerprint(second)
    context = build_proposal_context(
        operation_id=case.operation.operation_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PREPARED,
    )
    assert context.proposal_id is None
    assert context.next_order_session.isoformat() == "2026-10-15"
    actual = context.calculations["1234"]["buy_at_actual_quote"]
    conditional = context.calculations["1234"]["calculation_at_maximum_price"]
    assert actual["current_price_above_maximum"] is True
    assert conditional["hard_blockers"] == []
    assert context.calculations["1234"]["maximum_common_grid_price_yen"] == "1000"


def test_null_sell_reference_all_active_and_no_unexecuted_sell_proceeds(tmp_path):
    case = backend_case(tmp_path, holding=True, active_sell=True)
    inputs = _collect(case)
    assert len(inputs.active_reservations) == 1
    assert inputs.active_reservations[0].decision_reference is None
    assert inputs.active_reservations[0].reserved_yen == 0
    assert inputs.available_cash_yen == Decimal(210000)
    assert inputs.total_capital_yen == Decimal(320000)
    assert inputs.positions[0].original_assessment_ref is None
    assert "現在評価では補完しない" in inputs.positions[0].original_reason_unavailable_reason
    proposal = reviewed_proposal(case, action="hold")
    assert (
        TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
        .publish(proposal)
        .items[0]
        .action
        == "hold"
    )


def test_missing_quote_keeps_quantity_cost_and_unknown_nav(tmp_path):
    case = backend_case(tmp_path, holding=True, broken=True)
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=NULL")
    inputs = _collect(case)
    assert inputs.quotes == ()
    assert inputs.positions[0].quantity == 100
    assert inputs.positions[0].cost_yen == Decimal(90000)
    assert not inputs.positions[0].quantity_basis_confirmed
    assert inputs.total_capital_yen is None
    proposal = reviewed_proposal(case, action="sell")
    assert proposal.items[0].terms is None
    assert (
        TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
        .publish(proposal)
        .items[0]
        .action
        == "sell"
    )


def test_hold_case_intact_can_use_old_valuation_without_same_day_return_claim(tmp_path):
    case = backend_case(tmp_path, holding=True, assessment_as_of="2026-10-13")
    raw = reviewed_proposal(case, action="hold").payload()
    raw["items"][0]["return_calculation_unavailable_reason"] = (
        "旧日起点のため当日収益が十分とは主張しない。"
    )
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    assert (
        TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
        .publish(proposal)
        .items[0]
        .holding_basis
        == "investment_case_intact"
    )


@pytest.mark.parametrize(
    ("assessment_as_of", "valuation_unresolved", "blocker_code"),
    [
        ("2026-10-13", False, "valuation_basis_requires_refresh"),
        ("2026-10-14", True, "valuation_unresolved"),
    ],
)
def test_intact_hold_with_sufficient_reward_still_requires_current_resolved_arithmetic(
    tmp_path, assessment_as_of, valuation_unresolved, blocker_code
):
    case = backend_case(
        tmp_path,
        holding=True,
        assessment_as_of=assessment_as_of,
        valuation_unresolved=valuation_unresolved,
    )
    raw = reviewed_proposal(case, action="hold").payload()
    raw["items"][0]["remaining_reward"] = {
        "status": "sufficient",
        "reason": "合成の残存見返りは十分と判断した。",
    }
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    validation = service.check(proposal)
    assert not validation.valid
    assert blocker_code in {reason.code for reason in validation.blockers}
    before = case.app.read_bytes()
    with pytest.raises(ProposalError, match="同日起点 valuation と現在算術が必要"):
        service.publish(proposal)
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("holding_basis", ["investment_case_intact", "sufficient_remaining_reward"])
def test_same_day_sufficient_reward_is_verified_for_each_hold_basis(tmp_path, holding_basis):
    case = backend_case(tmp_path, holding=True)
    raw = reviewed_proposal(case, action="hold").payload()
    raw["items"][0]["holding_basis"] = holding_basis
    raw["items"][0]["remaining_reward"] = {
        "status": "sufficient",
        "reason": "同日起点の価値と現在quoteから残存見返りを確認した。",
    }
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(proposal).valid
    assert service.publish(proposal).items[0].holding_basis == holding_basis


def test_inputs_reject_future_ledger_meta_or_events(tmp_path):
    case = backend_case(tmp_path)
    with pytest.raises(ProposalError, match="ledger contains facts after"):
        _collect(case, now=PREPARED - timedelta(hours=2))


def test_expired_active_sell_reservation_is_retained(tmp_path):
    case = backend_case(tmp_path, holding=True, active_sell=True)
    inputs = _collect(case, now=PUBLISHED + timedelta(days=2))
    assert len(inputs.active_reservations) == 1
    assert inputs.active_reservations[0].decision_reference is None
    assert inputs.available_cash_yen == Decimal(210000)


def test_scaffold_keeps_signal_integration_as_authoring_not_a_price_gate(tmp_path):
    case = backend_case(tmp_path)
    raw = scaffold_proposal(
        operation_id=case.operation.operation_id,
        proposal_id=PROPOSAL_ID,
        db_path=case.app,
        sqlite_path=case.market,
        now=PREPARED,
    )
    item = raw["items"][0]
    assert item["action"] == "defer"
    assert item["assessment_ref"]["assessment_id"] == case.pair.assessment_id
    assert [reason["code"] for reason in item["blockers"]] == ["proposal_content_requires_review"]
    assert "モデル観測・一次資料" in item["rationale"]
    assert any("全シグナルのコメントは要求しない" in text for text in item["review_conditions"])
    assert raw["review"]["draft_sha256"] == "0" * 64


def test_partial_buy_uses_saved_hope_ceiling_cash_and_never_creates_binding(tmp_path):
    case = backend_case(tmp_path, cash_yen=95000)
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("DELETE FROM jquants_master_snapshots")
    raw = scaffold_proposal(
        operation_id=case.operation.operation_id,
        proposal_id=PROPOSAL_ID,
        db_path=case.app,
        sqlite_path=case.market,
        now=PREPARED,
    )
    item = raw["items"][0]
    item.pop("blockers")
    item.pop("resume_conditions")
    item.update(
        action="buy",
        timing="wait_limit",
        terms=None,
        proposed_limit_yen="900",
        terms_unavailable_reasons=[
            {
                "code": "security_classification_unavailable",
                "message": "分類未確認のため具体条件は未確定。",
            }
        ],
    )
    raw["review"]["reviewer_identity"] = "synthetic-independent-reviewer"
    proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(proposal).valid
    assert service.publish(proposal).items[0].proposed_limit_yen == Decimal(900)
    with closing(sqlite3.connect(case.app)) as connection, connection:
        assert (
            connection.execute("SELECT count(*) FROM position_decision_reference").fetchone()[0]
            == 0
        )
    raw["proposal_id"] = "investment-proposal-00000000-0000-4000-8000-000000000009"
    raw["items"][0]["proposed_limit_yen"] = "1000"
    too_expensive = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    assert not service.check(too_expensive).valid


@pytest.mark.parametrize(
    ("reservation_at", "execution_at", "occurred_on", "unavailable_reason"),
    [
        ("2026-10-15T09:00:00+09:00", "2026-10-15T09:01:00+09:00", None, None),
        ("2026-10-14T09:00:00+09:00", "2026-10-14T09:01:00+09:00", None, "評価公開は買付より後"),
        ("2026-10-14T16:09:00+09:00", "2026-10-14T16:10:00+09:00", None, "提案公開は買付より後"),
        ("2026-10-14T16:19:00+09:00", "2026-10-14T07:20:00Z", None, None),
        ("2026-10-14T09:00:00+09:00", "2026-10-14T09:01:00+09:00", "2026-10-14", None),
        ("2026-10-15T09:00:00+09:00", "2026-10-15T09:01:00+09:00", "2026-10-15", None),
        (
            "2026-10-13T09:00:00+09:00",
            "2026-10-13T09:01:00+09:00",
            "2026-10-13",
            "評価公開は買付より後",
        ),
    ],
)
def test_original_reason_respects_reported_buy_time_precision(
    tmp_path, reservation_at, execution_at, occurred_on, unavailable_reason
):
    # The earlier-day negative case needs cash to exist before its reported fill.
    case = backend_case(tmp_path, opening_at="2026-10-13T09:00:00+09:00")
    proposal = reviewed_proposal(case)
    proposal = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(proposal)
    from baibai_engine.portfolio.ledger.events import ExecutionEvent, ReservationEvent
    from baibai_engine.portfolio.ledger.store import LedgerStoreService

    store = LedgerStoreService(case.app)
    ledger, head = store.load_with_head()
    reservation = ReservationEvent.model_validate(
        {
            "type": "reservation",
            "event_id": "next-real-order",
            "reservation_id": "next-real-reservation",
            "order_id": "next-broker-order",
            "occurred_at": reservation_at,
            "ticker": "1234",
            "side": "buy",
            "sector": "情報・通信業",
            "common_factors": ["domestic-demand"],
            "decision_reference": proposal.proposal_id,
            "quantity": 100,
            "price_guard_yen": "1000",
            "expires_at": "2026-10-15T15:30:00+09:00",
        }
    )
    execution = ExecutionEvent.model_validate(
        {
            "type": "execution",
            "event_id": "next-real-fill",
            "execution_id": "next-broker-fill",
            "occurred_at": execution_at,
            "occurred_on": occurred_on,
            "ticker": "1234",
            "side": "buy",
            "reservation_id": reservation.reservation_id,
            "decision_reference": proposal.proposal_id,
            "quantity": 100,
            "price_yen": "1000",
        }
    )
    replacement = ledger.model_copy(
        update={
            "events": (*ledger.events, reservation, execution),
            "as_of": PUBLISHED + timedelta(days=1),
        }
    )
    store.apply_document(expected_head=head, expected_document=ledger, replacement=replacement)
    from tests.helpers.screening_sqlite import seed_daily_bars

    seed_daily_bars(case.market, [("1234", "2026-10-15", 900.0, 1.0)])
    inputs = _collect(case, now=PUBLISHED + timedelta(days=1))
    assert inputs.prior_buy_execution_references == (execution.execution_id,)
    if unavailable_reason is not None:
        assert inputs.positions[0].original_assessment_ref is None
        assert unavailable_reason in inputs.positions[0].original_reason_unavailable_reason
    else:
        assert inputs.positions[0].original_assessment_ref == proposal.items[0].assessment_ref
        assert inputs.positions[0].original_reason_unavailable_reason is None
    assert (
        _collect(
            case, now=PUBLISHED + timedelta(days=1), proposal_id="other-new-identity"
        ).prior_buy_execution_references
        == ()
    )
    assert get_investment_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == proposal
    context = build_proposal_context(
        operation_id=case.operation.operation_id,
        proposal_id=proposal.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(days=1),
    )
    assert context.inputs.prior_buy_execution_references == (execution.execution_id,)
    assert context.inputs.positions[0].quantity == 100


@pytest.mark.parametrize(("kind", "unchanged"), [("3", True), ("1", False)])
def test_holding_quantity_uses_share_units_not_the_price_adjustment(tmp_path, kind, unchanged):
    from baibai_engine.portfolio.holdings.valuation import current_portfolio

    case = backend_case(tmp_path, holding=True)
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.executemany(
            "INSERT INTO jquants_daily_bars "
            "(ticker, traded_at, close, adjustment_factor, ex_right_type) VALUES (?, ?, ?, ?, ?)",
            [
                ("1234", "2026-10-15", 1000.0, 0.8, kind),
                ("1234", "2026-10-16", 1000.0, 1.0, None),
            ],
        )
    now = PUBLISHED + timedelta(days=2)
    inputs = _collect(case, now=now)
    assert inputs.positions[0].quantity == 100
    assert inputs.positions[0].cost_yen == Decimal(90000)
    assert inputs.positions[0].quantity_basis_confirmed is unchanged
    assert inputs.total_capital_yen == (Decimal(310000) if unchanged else None)
    portfolio = current_portfolio(case.ledger, sqlite_path=case.market, now=now)
    assert portfolio.holdings[0].quantity == 100
    assert portfolio.holdings[0].market_value_yen == (100000 if unchanged else None)


@pytest.mark.parametrize(("product_category", "expected_common"), [("011", True), ("014", False)])
def test_classification_uses_native_product_not_sector(tmp_path, product_category, expected_common):
    case = backend_case(tmp_path)
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute(
            "UPDATE jquants_master_snapshots SET record_json = "
            "json_set(record_json, '$.ProdCat', ?, '$.S33Nm', '新業種') "
            "WHERE security_code = '12340'",
            (product_category,),
        )
    classification = next(
        row for row in _collect(case).security_classification if row.ticker == "1234"
    )
    assert classification.model_dump(mode="json") == {
        "ticker": "1234",
        "market_segment": "Prime",
        "is_common_stock": expected_common,
        "snapshot_date": "2026-10-14",
    }
    context = build_proposal_context(
        operation_id=case.operation.operation_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PREPARED,
    )
    assert context.calculations["1234"]["maximum_common_grid_price_yen"] == (
        "1000" if expected_common else None
    )


@pytest.mark.parametrize("consumer", ["current_portfolio", "proposal"])
def test_current_valuation_keeps_quotes_and_share_basis_in_one_snapshot(
    tmp_path, monkeypatch, consumer
):
    from tests.helpers.screening_sqlite import seed_daily_bars

    from baibai_engine.portfolio.holdings import valuation

    case = backend_case(tmp_path, holding=True)
    seed_daily_bars(
        case.market,
        [("1234", "2026-10-15", 1000.0, 1.0), ("5678", "2026-10-15", 1100.0, 1.0)],
    )
    with closing(sqlite3.connect(case.market)) as setup:
        assert setup.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    now = PUBLISHED + timedelta(days=1)
    read_quote = valuation.read_unadjusted_close
    commits = 0

    def commit_after_holding_quote(**kwargs):
        nonlocal commits
        quote = read_quote(**kwargs)
        with closing(sqlite3.connect(case.market)) as writer, writer:
            writer.execute(
                "UPDATE jquants_daily_bars SET close = 9999.0, "
                "adjustment_factor = 0.5, ex_right_type = '1' WHERE traded_at = '2026-10-15'"
            )
        commits += 1
        return quote

    monkeypatch.setattr(valuation, "read_unadjusted_close", commit_after_holding_quote)
    if consumer == "current_portfolio":
        portfolio = valuation.current_portfolio(case.ledger, sqlite_path=case.market, now=now)
        assert portfolio.holdings[0].market_value_yen == 100000
        assert portfolio.total_capital_yen == 310000
        assert portfolio.ledger_as_of == case.ledger.as_of
    else:
        inputs = _collect(case, now=now, tickers=("1234", "5678"))
        assert inputs.positions[0].quantity_basis_confirmed
        assert inputs.total_capital_yen == Decimal(310000)
        # The next candidate's quote also belongs to the holding's pre-commit snapshot.
        assert [(quote.ticker, quote.price_yen) for quote in inputs.quotes] == [
            ("1234", Decimal(1000)),
            ("5678", Decimal(1100)),
        ]
    assert commits == 1
    monkeypatch.setattr(valuation, "read_unadjusted_close", read_quote)
    quote, confirmed = valuation.holding_quote(
        case.ledger, ticker="1234", sqlite_path=case.market, now=now
    )
    assert quote is not None
    assert quote.close_yen == 9999.0
    assert not confirmed


@pytest.mark.parametrize(
    ("consumer", "action"),
    [
        ("context", "buy"),
        ("context", "hold"),
        ("check", "buy"),
        ("check", "hold"),
        ("preview", "buy"),
        ("preview", "hold"),
        ("preview", "no_action"),
    ],
)
def test_assessment_basis_uses_quote_snapshot_across_wal_correction(
    tmp_path, monkeypatch, consumer, action
):
    from baibai_engine.investment_judgment.trade_proposal import inputs as input_reader
    from baibai_engine.investment_judgment.trade_proposal import service as proposal_service
    from baibai_engine.investment_judgment.trade_proposal.query import preview_current_terms

    case = backend_case(tmp_path, holding=action == "hold", assessment_quote_as_of="2026-10-13")
    proposal = reviewed_proposal(case, action=action)
    if action == "hold":
        raw = proposal.payload()
        raw["items"][0]["remaining_reward"] = {
            "status": "sufficient",
            "reason": "同日起点の現在算術による残存見返りを主張する。",
        }
        proposal = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    if consumer == "preview":
        proposal = service.publish(proposal)
    with closing(sqlite3.connect(case.market)) as writer, writer:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        writer.execute("UPDATE jquants_daily_bars SET adjustment_factor = 0.5, ex_right_type = '1'")

    collect = input_reader.collect_proposal_inputs
    observed = []

    def collect_then_correct(*args, **kwargs):
        inputs = collect(*args, **kwargs)
        observed.append(inputs)
        with closing(sqlite3.connect(case.market)) as writer, writer:
            writer.execute(
                "UPDATE jquants_daily_bars SET close = 2200, "
                "adjustment_factor = 1.0, ex_right_type = NULL"
            )
        return inputs

    owner = proposal_service if consumer == "check" else input_reader
    monkeypatch.setattr(owner, "collect_proposal_inputs", collect_then_correct)
    if consumer == "context":
        context = build_proposal_context(
            operation_id=case.operation.operation_id,
            db_path=case.app,
            sqlite_path=case.market,
            now=PUBLISHED,
        )
        row = context.calculations["1234"]
        assert row["current_quote_projection"] is None
        if action == "buy":
            result = row["buy_at_actual_quote"]
            assert result["current_price_projection"] is None
            assert result["quantity"] == 0
            assert "corporate_action_unresolved" in {row["code"] for row in result["hard_blockers"]}
    elif consumer == "check":
        validation = service.check(proposal)
        assert not validation.valid
        assert "corporate_action_unresolved" in {reason.code for reason in validation.blockers}
    else:
        preview = preview_current_terms(
            proposal_id=proposal.proposal_id,
            db_path=case.app,
            sqlite_path=case.market,
            now=PUBLISHED,
        )
        result = preview.items[0]["current_quote_calculation"]
        if action == "no_action":
            # An unavailable current projection does not invalidate an economic
            # decision to do nothing, nor rewrite its saved assessment.
            assert preview.status == "unchanged"
            assert result is None
        else:
            assert preview.status == "blocked"
            assert result["current_price_projection"] is None
            assert "corporate_action_unresolved" in {
                reason["code"] for reason in preview.items[0]["blockers"]
            }
            if action == "buy":
                assert Decimal(result["calculation_price_yen"]) == Decimal(1100)
                assert result["quantity"] == 0
            else:
                assert result["current_position"]["quantity"] == 100
                assert result["current_position"]["quantity_basis_confirmed"]
                assert (
                    result["return_calculation_unavailable_reason"] == "corporate_action_unresolved"
                )
        assert (
            get_investment_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == proposal
        )
    assert len(observed) == 1
    assert observed[0].quotes[0].price_yen == Decimal(1100)
    monkeypatch.setattr(owner, "collect_proposal_inputs", collect)
    # A separate evaluation sees all of the correction, not the old cheap quote
    # combined with the corrected unit factor. The saved assessment is unchanged.
    corrected = build_proposal_context(
        operation_id=case.operation.operation_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED,
    )
    assert corrected.inputs.quotes[0].price_yen == Decimal(2200)
    projection = corrected.calculations["1234"]["current_quote_projection"]
    assert projection["price_yen"] == 2200
    assert corrected.assessments[case.pair.assessment_id] == case.pair


@pytest.mark.parametrize(("kind", "unchanged"), [("1", False), ("2", False), ("3", True)])
def test_known_intraday_action_without_new_fill_reaches_sell_quantity_gate(
    tmp_path, kind, unchanged
):
    from baibai_engine.investment_judgment.trade_proposal.conditions import (
        terms_unavailable_reasons,
    )

    case = backend_case(tmp_path, holding=True, broken=True)
    proposal = reviewed_proposal(case, action="sell")
    now = (PUBLISHED + timedelta(days=1)).replace(hour=10)
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute(
            "INSERT INTO jquants_daily_bars "
            "(ticker, traded_at, close, adjustment_factor, ex_right_type) "
            "VALUES ('1234', '2026-10-15', NULL, 0.5, ?)",
            (kind,),
        )
    inputs = _collect(case, now=now)
    assert inputs.quotes[0].price_as_of == PREPARED.date()
    assert inputs.quotes[0].price_yen == Decimal(1100)
    assert inputs.positions[0].quantity == 100
    assert inputs.positions[0].cost_yen == Decimal(90000)
    assert inputs.positions[0].quantity_basis_confirmed is unchanged
    assert inputs.available_cash_yen == Decimal(210000)
    assert inputs.total_capital_yen == (Decimal(320000) if unchanged else None)
    reasons = terms_unavailable_reasons(proposal.items[0], inputs, sqlite_path=case.market, now=now)
    assert {reason.code for reason in reasons} == (
        set() if unchanged else {"quantity_basis_unconfirmed"}
    )


@pytest.mark.parametrize("consumer", ["context", "check", "preview"])
def test_classification_and_order_session_share_the_quote_snapshot(tmp_path, monkeypatch, consumer):
    from baibai_engine.investment_judgment.trade_proposal import inputs as input_reader
    from baibai_engine.investment_judgment.trade_proposal.query import preview_current_terms

    case = backend_case(tmp_path)
    proposal = reviewed_proposal(case)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    if consumer == "preview":
        proposal = service.publish(proposal)
    with closing(sqlite3.connect(case.market)) as setup:
        assert setup.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    read_quote = input_reader.read_unadjusted_close
    commits = 0

    def quote_then_correct(**kwargs):
        nonlocal commits
        quote = read_quote(**kwargs)
        with closing(sqlite3.connect(case.market)) as writer, writer:
            writer.execute("UPDATE jquants_daily_bars SET close = 2200")
            writer.execute(
                "UPDATE jquants_master_snapshots "
                "SET record_json = json_set(record_json, '$.ProdCat', '014')"
            )
            writer.execute(
                "UPDATE jquants_market_calendar SET holiday_division = '0' WHERE day = '2026-10-15'"
            )
        commits += 1
        return quote

    monkeypatch.setattr(input_reader, "read_unadjusted_close", quote_then_correct)
    if consumer == "context":
        context = build_proposal_context(
            operation_id=case.operation.operation_id,
            db_path=case.app,
            sqlite_path=case.market,
            now=PUBLISHED,
        )
        assert context.inputs.quotes[0].price_yen == Decimal(1100)
        assert context.inputs.security_classification[0].is_common_stock
        assert context.next_order_session.isoformat() == "2026-10-15"
        assert context.calculations["1234"]["maximum_common_grid_price_yen"] == "1000"
    elif consumer == "check":
        assert service.check(proposal).valid
    else:
        preview = preview_current_terms(
            proposal_id=proposal.proposal_id,
            db_path=case.app,
            sqlite_path=case.market,
            now=PUBLISHED,
        )
        assert preview.status == "unchanged"
        assert preview.items[0]["blockers"] == []
        assert (
            get_investment_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == proposal
        )
    assert commits == 1
    monkeypatch.setattr(input_reader, "read_unadjusted_close", read_quote)
    # The next independent read sees the whole correction. No reader closes or
    # commits its caller's snapshot, and no saved proposal is rewritten.
    corrected = build_proposal_context(
        operation_id=case.operation.operation_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED,
    )
    assert corrected.inputs.quotes[0].price_yen == Decimal(2200)
    assert not corrected.inputs.security_classification[0].is_common_stock
    assert corrected.next_order_session.isoformat() == "2026-10-16"
    assert corrected.calculations["1234"]["maximum_common_grid_price_yen"] is None
