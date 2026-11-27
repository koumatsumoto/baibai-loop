"""売却下限を約定規模とせず、全量回収の既存ADV注意を保存する。"""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

import pytest
from tests.helpers import proposal as fixtures
from tests.helpers.proposal import PUBLISHED, backend_case, bind_review, read_saved_proposal
from tests.helpers.proposal import reviewed_proposal as draft_for
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.investment_judgment.trade_proposal import service as service_owner
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    GeneratedLiquidityContext,
    ProposalConflictError,
)
from baibai_engine.investment_judgment.trade_proposal.preview import preview_current_terms
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.investment_judgment.trade_proposal.warnings import build_proposal_warnings
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.portfolio.ledger.drafts import apply_draft, build_share_conversion_draft
from baibai_engine.portfolio.ledger.store import LedgerStoreService


def _sell(case, *, limit=1000):
    raw = draft_for(case, action="sell").payload()
    raw["items"][0].update(
        terms={
            "side": "sell",
            "limit_price_yen": str(limit),
            "quantity": 100,
            "notional_yen": str(limit * 100),
            "target_session": "2026-10-15",
            "valuation_as_of": "2026-10-14",
            "maximum_entry_price_yen": None,
            "price_grid_rule_id": "tse-common-conservative-20261014",
            "conditions": ["合成の全量回収。下限・ADVとも約定や即時cash化を保証しない。"],
            "warnings": [],
        },
        terms_unavailable_reasons=[],
    )
    return bind_review(raw, {case.pair.assessment_id: case.pair.document})


def _liquidity(adv):
    return (
        GeneratedLiquidityContext(
            ticker="1234",
            generation_id="synthetic-liquidity",
            analysis_as_of=date(2026, 10, 14),
            calculation_revision="synthetic-market-v1",
            price_window_end=date(2026, 10, 14),
            adv_yen=adv,
            unavailable_reason="合成欠測" if adv is None else None,
        ),
    )


@pytest.fixture(scope="module")
def complete_sell(tmp_path_factory):
    case = backend_case(tmp_path_factory.mktemp("sell-warning"), holding=True, broken=True)
    return _sell(case)


@pytest.mark.parametrize("limit", [500, 1100, 2000])
def test_sell_adv_uses_quote_times_saved_quantity_not_author_floor(tmp_path, limit):
    case = backend_case(tmp_path, holding=True, broken=True)
    proposal = _sell(case, limit=limit)
    before = proposal.model_dump_json()
    result = build_proposal_warnings(
        proposal.inputs, proposal.items, _liquidity(Decimal(2000000)), assessments={}
    )
    # 1100円 * 100株 / 200万円 = 5.5%。下限500円の2.5%へ置き換えない。
    assert result.terms_warnings["1234"] == ("adv_participation_exceeds_warning",)
    assert result.portfolio_exposure.ticker is None
    assert result.portfolio_exposure.sector is None
    assert proposal.model_dump_json() == before


@pytest.mark.parametrize("adv", [None, Decimal(0), Decimal(-1)])
def test_sell_unknown_or_nonpositive_adv_is_not_zero_risk(complete_sell, adv):
    result = build_proposal_warnings(
        complete_sell.inputs, complete_sell.items, _liquidity(adv), assessments={}
    )
    assert result.terms_warnings["1234"] == ("adv_participation_unassessed",)


@pytest.mark.parametrize(
    "unknown", ["quote_missing", "quote_stale", "quote_future", "basis", "position_missing"]
)
def test_sell_unconfirmed_reference_notional_is_unassessed(complete_sell, unknown):
    quote = complete_sell.inputs.quotes[0]
    positions = complete_sell.inputs.positions
    if unknown == "position_missing":
        positions = ()
    elif unknown == "basis":
        positions = (positions[0].model_copy(update={"quantity_basis_confirmed": False}),)
    elif unknown != "quote_missing":
        quote = quote.model_copy(
            update={"price_as_of": date(2026, 10, 1 if unknown == "quote_stale" else 15)}
        )
    inputs = complete_sell.inputs.model_copy(
        update={"quotes": () if unknown == "quote_missing" else (quote,), "positions": positions}
    )
    result = build_proposal_warnings(
        inputs, complete_sell.items, _liquidity(Decimal(2000000)), assessments={}
    )
    assert result.terms_warnings["1234"] == ("adv_participation_unassessed",)


def test_sell_equality_is_warning_free_and_unconfirmed_terms_do_not_invent_scale(tmp_path):
    case = backend_case(tmp_path, holding=True, broken=True)
    proposal = _sell(case)
    result = build_proposal_warnings(
        proposal.inputs, proposal.items, _liquidity(Decimal(2200000)), assessments={}
    )
    assert result.terms_warnings["1234"] == ()
    incomplete = draft_for(case, action="sell")
    result = build_proposal_warnings(
        incomplete.inputs, incomplete.items, _liquidity(Decimal(1)), assessments={}
    )
    assert result.terms_warnings == {}


def _case_with_liquidity(tmp_path):
    model = GeneratedModel(
        model_id="market_context",
        subject="1234",
        calculation_revision="synthetic-market-v1",
        payload={"price": {"avg_turnover_20d_oku": 0.02, "resolved_date": "2026-10-14"}},
    )
    return backend_case(tmp_path, holding=True, broken=True, models=(model,))


def test_sell_warning_follows_normal_check_save_exact_retry_and_preview(tmp_path):
    case = _case_with_liquidity(tmp_path)
    proposal = _sell(case, limit=500)
    assert proposal.items[0].terms.warnings == ("adv_participation_exceeds_warning",)
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    store = LedgerStoreService(case.app)
    ledger_before = store.load_with_head()
    assert service.check(proposal).valid  # 流動性注意は売却停止gateではない。
    saved = service.publish(proposal)
    before = case.app.read_bytes()
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert service.publish(proposal) == saved
    preview = preview_current_terms(
        proposal_id=saved.proposal_id, db_path=case.app, sqlite_path=case.market, now=PUBLISHED
    )
    assert preview.items[0]["current_terms_warnings"] == ["adv_participation_exceeds_warning"]
    assert preview.items[0]["blockers"] == []
    assert preview.items[0]["saved_terms"]["quantity"] == 100
    assert preview.current_inputs.available_cash_yen == proposal.inputs.available_cash_yen
    assert case.app.read_bytes() == before
    assert store.load_with_head() == ledger_before


def test_legacy_saved_sell_without_warning_remains_exact_but_preview_adds_current_attention(
    tmp_path, monkeypatch
):
    case = _case_with_liquidity(tmp_path)

    def legacy(*args, **kwargs):
        result = build_proposal_warnings(*args, **kwargs)
        return replace(result, terms_warnings={})

    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    with monkeypatch.context() as old:
        old.setattr(fixtures, "build_proposal_warnings", legacy)
        old.setattr(service_owner, "build_proposal_warnings", legacy)
        proposal = _sell(case)
        saved = service.publish(proposal)
    assert saved.items[0].terms.warnings == ()
    with pytest.raises(ProposalConflictError, match="terms warnings differ"):
        service.check(proposal)
    before = case.app.read_bytes()
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert service.publish(proposal) == saved
    preview = preview_current_terms(
        proposal_id=saved.proposal_id, db_path=case.app, sqlite_path=case.market, now=PUBLISHED
    )
    assert preview.items[0]["saved_terms"]["warnings"] == []
    assert preview.items[0]["current_terms_warnings"] == ["adv_participation_exceeds_warning"]
    assert case.app.read_bytes() == before


@pytest.mark.parametrize("share_basis", ["unchanged", "unconfirmed_split", "confirmed_split"])
def test_sell_preview_does_not_mix_saved_full_quantity_with_current_share_units(
    tmp_path, share_basis
):
    case = _case_with_liquidity(tmp_path)
    proposal = _sell(case)
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(proposal)
    now = datetime.fromisoformat("2026-10-15T16:20:00+09:00")
    split = share_basis != "unchanged"
    quote = 550 if split else 1100
    seed_daily_bars(case.market, [("1234", "2026-10-15", quote, 0.5 if split else 1.0)])
    if split:
        with closing(sqlite3.connect(case.market)) as connection, connection:
            connection.execute(
                "UPDATE jquants_daily_bars SET ex_right_type='1' WHERE traded_at='2026-10-15'"
            )
    store = LedgerStoreService(case.app)
    confirmed = share_basis == "confirmed_split"
    if confirmed:
        draft = build_share_conversion_draft(
            store,
            action_id="synthetic-split",
            ticker="1234",
            basis_date=now.date(),
            numerator=2,
            denominator=1,
            quantity_before=100,
            quantity_after=200,
            confirmed_at=now,
            source_ref="synthetic:human-confirmed",
            now=now,
        )
        assert draft is not None
        apply_draft(store, draft, human_confirmed=True, now=now)
    before = case.app.read_bytes()
    preview = preview_current_terms(
        proposal_id=saved.proposal_id, db_path=case.app, sqlite_path=case.market, now=now
    )
    current = preview.current_inputs
    assert current.positions[0].quantity_basis_confirmed is (share_basis != "unconfirmed_split")
    assert current.positions[0].quantity == (200 if confirmed else 100)
    assert current.quotes[0].price_yen == quote
    assert current.available_cash_yen == saved.inputs.available_cash_yen
    row = preview.items[0]
    assert row["saved_terms"]["quantity"] == 100
    assert row["saved_terms"]["warnings"] == ["adv_participation_exceeds_warning"]
    assert ("quantity_basis_unconfirmed" in {reason["code"] for reason in row["blockers"]}) is split
    assert row["current_terms_warnings"] == [
        "adv_participation_unassessed" if split else "adv_participation_exceeds_warning"
    ]
    assert case.app.read_bytes() == before
    assert read_saved_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
