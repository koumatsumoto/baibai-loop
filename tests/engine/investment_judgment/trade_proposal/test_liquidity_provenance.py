"""固定の流動性根拠による新規発行と、既発行の履歴。合成storeのみ。"""

import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from hashlib import sha256

import pytest
import yaml
from tests.helpers import proposal as helper
from tests.helpers.proposal import PROPOSAL_ID

from baibai_engine.cli.investment_judgment import main as investment_cli
from baibai_engine.investment_judgment.trade_proposal import service as service_owner
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    ProposalConflictError,
    load_proposal_draft,
    proposal_draft_sha256,
)
from baibai_engine.investment_judgment.trade_proposal.preview import preview_current_terms
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.models.market_context.security import (
    calculate_security_market_context,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.prices.price_observation import price_calendar
from baibai_engine.primitives.json import canonical_json


@pytest.fixture
def case(tmp_path, request):
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
        calendar=price_calendar({}, as_of=helper.PREPARED.date()),
        master=None,
        benchmark_ticker="1321",
        benchmark_prices={},
        peer_returns=(),
    )
    for field in getattr(request, "param", ()):
        market["price"].pop(field)
    model = GeneratedModel(
        model_id="market_context",
        subject="1234",
        calculation_revision="synthetic-v1",
        payload=market,
    )
    return helper.backend_case(tmp_path, models=(model,))


def _signed_omission(case, omitted_fields):
    raw = helper.reviewed_proposal(case).payload()
    for field in omitted_fields:
        raw["liquidity_context"][0].pop(field)
    # 旧serializer相当の本文を直接hashして、現行の型のdefaultに依存しない。
    raw["review"]["draft_sha256"] = sha256(
        canonical_json({k: v for k, v in raw.items() if k != "review"}).encode()
    ).hexdigest()
    proposal = load_proposal_draft(raw)
    assert proposal.payload() == raw
    assert proposal_draft_sha256(proposal) == raw["review"]["draft_sha256"]
    return proposal


def test_current_cli_context_and_draft_keep_saved_windows_and_publish(case, tmp_path, capsys):
    path = tmp_path / "proposal.yaml"
    assert (
        investment_cli(
            [
                "trade-proposal-context",
                "--proposal-id",
                PROPOSAL_ID,
                "--operation-id",
                case.operation.operation_id,
                "--db",
                str(case.app),
                *case.listing_args,
                "--sqlite-path",
                str(case.market),
                "--out",
                str(path),
            ],
            now=helper.PREPARED,
        )
        == 0
    )
    context = json.loads(capsys.readouterr().out)
    row = context["liquidity_context"][0]
    assert yaml.safe_load(path.read_text())["liquidity_context"] == [row]
    assert row["price_window_end"] == "2026-10-07"
    assert row["analysis_as_of"] == "2026-10-14"
    assert row["adv_yen"] == "100000000.0"
    assert row["turnover_window"] == {
        "start_date": "2026-09-10",
        "end_date": "2026-10-07",
        "price_bar_count": 20,
        "valid_turnover_count": 19,
        "median_yen": "100000000.0",
    }
    proposal = helper.reviewed_proposal(case)
    assert proposal.payload()["liquidity_context"] == [row]
    service = TradeProposalService(
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: helper.PUBLISHED,
    )
    assert service.check(proposal).valid
    assert service.publish(proposal).liquidity_context == proposal.liquidity_context


@pytest.mark.parametrize(
    "omitted_fields",
    [("price_window_end",), ("turnover_window",), ("price_window_end", "turnover_window")],
)
def test_source_present_windows_cannot_be_omitted_from_a_new_draft(case, omitted_fields):
    proposal = _signed_omission(case, omitted_fields)
    service = TradeProposalService(
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: helper.PUBLISHED,
    )
    with pytest.raises(ProposalConflictError, match="liquidity differs"):
        service.check(proposal)
    with pytest.raises(ProposalConflictError, match="liquidity differs"):
        service.publish(proposal)
    with closing(sqlite3.connect(case.app)) as connection:
        assert connection.execute("SELECT count(*) FROM investment_proposal").fetchone() == (0,)


@pytest.mark.parametrize(
    "case",
    [("resolved_date",), ("turnover_window",), ("resolved_date", "turnover_window")],
    indirect=True,
)
def test_new_draft_can_publish_with_genuinely_missing_source_fields(case):
    proposal = helper.reviewed_proposal(case)
    price = case.selected.inputs.generation.result("market_context", "1234").payload["price"]
    observed = proposal.payload()["liquidity_context"][0]
    assert ("price_window_end" in observed) == ("resolved_date" in price)
    assert ("turnover_window" in observed) == ("turnover_window" in price)
    service = TradeProposalService(
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: helper.PUBLISHED,
    )
    assert service.check(proposal).valid
    assert service.publish(proposal).liquidity_context == proposal.liquidity_context


@pytest.mark.parametrize(
    "omitted_fields", [("turnover_window",), ("price_window_end", "turnover_window")]
)
def test_existing_legacy_proposal_is_read_and_retried_without_rewriting(
    case, omitted_fields, monkeypatch
):
    proposal = _signed_omission(case, omitted_fields)
    clock = helper.PUBLISHED
    service = TradeProposalService(
        case.app, listing_reader=case.listing_reader, sqlite_path=case.market, clock=lambda: clock
    )
    # 旧writerの窓の照合だけを模擬し、保存された本文・hash・索引には触れない。
    with monkeypatch.context() as old_writer:
        old_writer.setattr(
            service_owner, "proposal_liquidity_context", lambda _: proposal.liquidity_context
        )
        saved = service.publish(proposal)
    with closing(sqlite3.connect(case.app)) as connection:
        before = connection.execute(
            "SELECT payload,draft_sha256 FROM investment_proposal"
        ).fetchone()
        fixed_selection = connection.execute("SELECT payload FROM operation_session").fetchall()
    assert json.loads(before[0]) == saved.payload()
    assert before[1] == proposal_draft_sha256(proposal)
    assert all(field not in saved.payload()["liquidity_context"][0] for field in omitted_fields)
    case.runs.unlink()
    clock += timedelta(days=1)
    stored = helper.read_saved_proposal(proposal_id=proposal.proposal_id, db_path=case.app)
    assert stored == saved
    assert service.publish(proposal) == stored
    assert (
        preview_current_terms(
            proposal_id=proposal.proposal_id,
            db_path=case.app,
            listing_reader=case.listing_reader,
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
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: helper.PUBLISHED,
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
    assert row.turnover_window is None
    assert "turnover_window" not in row.model_dump(mode="json")
    assert (row.adv_yen is None) == (price is None)


@pytest.mark.parametrize(
    ("field", "value"), [("valid_turnover_count", 20), ("median_yen", "90000000")]
)
def test_proposal_keeps_the_saved_turnover_window(case, field, value):
    raw = helper.reviewed_proposal(case).payload()
    raw["liquidity_context"][0]["turnover_window"][field] = value
    changed = helper.bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(
        case.app,
        listing_reader=case.listing_reader,
        sqlite_path=case.market,
        clock=lambda: helper.PUBLISHED,
    )
    with pytest.raises(ProposalConflictError, match="liquidity differs"):
        service.check(changed)
