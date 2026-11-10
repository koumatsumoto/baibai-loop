"""価格窓終端の伝播と、日付を持たない保存提案の互換性。合成storeだけを使う。"""

import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from hashlib import sha256

import pytest
from tests.helpers import proposal as helper

from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalConflictError,
    load_proposal_draft,
    proposal_draft_sha256,
)
from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.preview import preview_current_terms
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.models.market_context.security import (
    calculate_security_market_context,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.primitives.json import canonical_json


@pytest.fixture
def case(tmp_path, monkeypatch):
    last = helper.PREPARED.date() - timedelta(days=7)
    days = sorted(
        (last - timedelta(days=i) for i in range(35) if (last - timedelta(days=i)).weekday() < 5),
        reverse=True,
    )[:20]
    # 最終barの売買代金が欠測でも、価格窓の終端と最後の有効売買代金日は別。
    bars = tuple(
        PriceBar(day, 1000.0, None if day == last else 100_000_000.0) for day in sorted(days)
    )
    market = calculate_security_market_context(
        ticker="1234",
        bars=bars,
        master=None,
        benchmark_ticker="1321",
        benchmark_prices={},
        peer_prices=(),
        self_range_window_bars=750,
    )
    model = GeneratedModel(
        model_id="market_context",
        subject="1234",
        calculation_revision="synthetic-v1",
        payload=market,
    )
    seed = helper.seed_generated_selection
    monkeypatch.setattr(
        helper, "seed_generated_selection", lambda **kw: seed(**kw, models=(model,))
    )
    return helper.backend_case(tmp_path)


@pytest.mark.parametrize("legacy_draft", [False, True])
def test_price_window_date_is_fixed_without_rewriting_legacy_proposals(case, legacy_draft):
    context = build_proposal_context(
        operation_id=case.operation.operation_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=helper.PREPARED,
    )
    row = context.payload()["liquidity_context"][0]
    assert row["price_window_end"] == "2026-10-07"
    assert row["analysis_as_of"] == "2026-10-14"
    assert row["adv_yen"] == "100000000.0"
    proposal = helper.reviewed_proposal(case)
    raw = proposal.payload()
    if legacy_draft:
        raw["liquidity_context"][0].pop("price_window_end")
        # 旧serializerの本文を直接hashし、新しい型のdefaultに依存しない。
        raw["review"]["draft_sha256"] = sha256(
            canonical_json({k: v for k, v in raw.items() if k != "review"}).encode()
        ).hexdigest()
        proposal = load_proposal_draft(raw)
    assert proposal.payload() == raw
    assert proposal_draft_sha256(proposal) == raw["review"]["draft_sha256"]
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: helper.PUBLISHED
    )
    assert service.check(proposal).valid
    saved = service.publish(proposal)
    assert saved.liquidity_context == proposal.liquidity_context
    with closing(sqlite3.connect(case.app)) as connection:
        before = connection.execute(
            "SELECT payload,draft_sha256 FROM investment_proposal"
        ).fetchone()
        fixed_selection = connection.execute("SELECT payload FROM operation_session").fetchall()
    assert json.loads(before[0])["liquidity_context"] == raw["liquidity_context"]
    assert before[1] == raw["review"]["draft_sha256"]
    # mutable runを失っても、readback/previewは保存済みcontextを変更しない。
    case.runs.unlink()
    assert helper.read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app) == saved
    assert (
        preview_current_terms(
            proposal_id=proposal.proposal_id,
            db_path=case.app,
            sqlite_path=case.market,
            now=helper.PUBLISHED + timedelta(minutes=5),
        ).status
        == "unchanged"
    )
    with closing(sqlite3.connect(case.app)) as connection:
        assert (
            connection.execute("SELECT payload,draft_sha256 FROM investment_proposal").fetchone()
            == before
        )
        assert (
            connection.execute("SELECT payload FROM operation_session").fetchall()
            == fixed_selection
        )


@pytest.mark.parametrize(
    ("legacy_draft", "field", "value"),
    [
        (False, "price_window_end", "2026-10-08"),
        (False, "adv_yen", "90000000"),
        (False, "generation_id", "other"),
        (True, "adv_yen", "90000000"),
        (True, "generation_id", "other"),
    ],
)
def test_saved_selection_still_rejects_changed_liquidity(case, legacy_draft, field, value):
    raw = helper.reviewed_proposal(case).payload()
    if legacy_draft:
        raw["liquidity_context"][0].pop("price_window_end")
    raw["liquidity_context"][0][field] = value
    changed = helper.bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: helper.PUBLISHED
    )
    with pytest.raises(ProposalConflictError, match="liquidity differs"):
        service.check(changed)


@pytest.mark.parametrize("price", [{"avg_turnover_20d_oku": 1.0}, None])
def test_missing_source_date_is_not_replaced_with_generation_date(case, price):
    from baibai_engine.investment_judgment.trade_proposal.inputs import proposal_liquidity_context

    generation = case.selected.inputs.generation
    model = generation.result("market_context", "1234")
    changed = model.model_copy(update={"payload": {"price": price}})
    generation = generation.model_copy(update={"models": (changed,)})
    selection = case.selected.model_copy(
        update={"inputs": case.selected.inputs.model_copy(update={"generation": generation})}
    )
    row = proposal_liquidity_context(selection)[0]
    assert row.price_window_end is None
    assert "price_window_end" not in row.model_dump(mode="json")
    assert (row.adv_yen is None) == (price is None)
