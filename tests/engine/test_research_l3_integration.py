"""五領域の合成入力から、既存の評価pairと行動理由を保存・再読する。

文章は手書きの合成goldenであり、独立した人間/AIによる意味のReview成功を表さない。
取得・L2算術の網羅検証や、価格指標の予測力の検証は既存owner/別受入へ残す。
"""

import socket
import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import yaml
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.db_seed import seed_ledger
from tests.helpers.l1_release import publish_market_fixture
from tests.helpers.master import insert_master_rows
from tests.helpers.proposal import PREPARED, PROPOSAL_ID, PUBLISHED, bind_review
from tests.helpers.research_selection import draft_for, selection
from tests.helpers.screening_run import stock_analysis
from tests.helpers.screening_sqlite import insert_financial_record, seed_daily_bars

from baibai_engine.foundation.json import canonical_json
from baibai_engine.investment_judgment.inputs import project_research_inputs
from baibai_engine.investment_judgment.market_context.models import MarketContextAssessment
from baibai_engine.investment_judgment.market_context.service import AssessmentService
from baibai_engine.investment_judgment.operation.progress import selection_binding
from baibai_engine.investment_judgment.operation.service import OperationService
from baibai_engine.investment_judgment.selection import selection_sha256
from baibai_engine.investment_judgment.stock_assessment import (
    AssessmentReview,
    StockAssessmentDocument,
    assessment_core_hash,
)
from baibai_engine.investment_judgment.stock_assessment_store import load_reviewed_assessment
from baibai_engine.investment_judgment.trade_proposal import (
    ProposalConflictError,
    TradeProposal,
    proposal_draft_sha256,
)
from baibai_engine.investment_judgment.trade_proposal_inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal_query import (
    get_investment_proposal,
    preview_current_terms,
)
from baibai_engine.investment_judgment.trade_proposal_scaffold import scaffold_proposal
from baibai_engine.investment_judgment.trade_proposal_service import TradeProposalService
from baibai_engine.investment_judgment.workspace import (
    ResearchWorkspaceDataError,
    prepare_selected_workspace,
    promote,
    resume_selected_workspace,
)
from baibai_engine.machine_analysis.external_assessment.observations.models import (
    AnalystForecastObservation,
    ModelValuationObservation,
)
from baibai_engine.machine_analysis.external_assessment.query import external_assessment
from baibai_engine.machine_analysis.external_assessment.store import save_observations
from baibai_engine.position.ledger import PortfolioLedgerDocument
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.storage.application.read import connect_read_only

START = datetime.fromisoformat("2026-10-14T15:45:00+09:00")
PROMOTED = datetime.fromisoformat("2026-10-14T16:06:00+09:00")
CONTEXT_ID = "market-context-assessment-2026-10-14-synthetic-rates"


def _external_observations(at, *, missing=False, target=1500):
    common = dict(ticker="1234", parser_version="synthetic-1")
    if missing:
        return [
            AnalystForecastObservation(
                **common,
                fetched_at_utc=at,
                source_url="https://example.invalid/minkabu/1234",
                availability="not_covered",
            )
        ]
    return [
        ModelValuationObservation(
            **common,
            fetched_at_utc=at - timedelta(seconds=index),
            distributor=distributor,
            origin="ifis",
            source_url=f"https://example.invalid/{distributor}/1234",
            consensus_target=target,
            consensus_target_origin="ifis",
            consensus_target_period="12-month-target",
            currency="JPY",
            unit="currency_per_share",
            share_basis="synthetic-common-shares",
        )
        for index, distributor in enumerate(("monex", "rakuten"))
    ]


def _prepare_case(tmp_path, monkeypatch, scenario="wait"):
    monkeypatch.setattr(socket, "socket", lambda *_a, **_kw: pytest.fail("Research used network"))
    app, market, runs = (tmp_path / name for name in ("app.sqlite", "market.sqlite", "runs.sqlite"))
    ledger = PortfolioLedgerDocument.model_validate(
        {
            "schema_version": 3,
            "portfolio_scope": "repository_only",
            "as_of": START.isoformat(),
            "events": [
                {
                    "type": "opening_balance",
                    "event_id": "synthetic-opening",
                    "occurred_at": (START - timedelta(hours=6)).isoformat(),
                    "amount_yen": 300000,
                }
            ],
        }
    )
    seed_ledger(app, ledger)
    quote = 900 if scenario == "current" else 1100
    count = 60 if scenario == "gaps" else 250
    previous = 800 if scenario == "broken" else 1200
    seed_daily_bars(
        market,
        [
            (
                "1234",
                (START.date() - timedelta(days=count - index - 1)).isoformat(),
                float(quote if index == count - 1 else previous),
                1.0,
            )
            for index in range(count)
        ],
    )
    with closing(sqlite3.connect(market)) as connection, connection:
        insert_master_rows(
            connection, [("2026-10-14", "1234", "合成企業", "Prime", "情報・通信業", "011")]
        )
        connection.executemany(
            "INSERT INTO jquants_market_calendar(day,holiday_division) VALUES (?,?)",
            [("2026-10-14", 1), ("2026-10-15", 1)],
        )
    # 同じ四半期の増収と利益率低下を別々に残す。成長スコアや将来予測ではない。
    if scenario != "gaps":
        with closing(sqlite3.connect(market)) as connection, connection:
            for year, sales, operating_profit in ((2025, 100, 20), (2026, 120, 18)):
                insert_financial_record(
                    connection,
                    {
                        "Code": "12340",
                        "DiscDate": f"{year}-08-14",
                        "DiscTime": "15:00:00",
                        "DiscNo": f"synthetic-growth-{year}-1",
                        "DocType": "FinancialStatements_Consolidated_JP",
                        "CurPerType": "1Q",
                        "CurPerSt": f"{year}-04-01",
                        "CurPerEn": f"{year}-06-30",
                        "CurFYEn": f"{year + 1}-03-31",
                        "Sales": sales,
                        "OP": operating_profit,
                        "NP": operating_profit / 2,
                    },
                )
    publish_market_fixture(market, "five-domain-fixed-input", created_at=START)
    save_observations(
        market, _external_observations(START - timedelta(minutes=1), missing=scenario == "gaps")
    )
    AssessmentService(app).publish(
        MarketContextAssessment.model_validate(
            {
                "context_id": CONTEXT_ID,
                "as_of": START.date(),
                "known_at": START - timedelta(minutes=3),
                "published_at": START - timedelta(minutes=2),
                "summary": "合成金利上昇。変動負債と借換への影響は個社の契約で異なる。",
                "inputs": [
                    {
                        "input_id": "rates",
                        "reference": "https://example.invalid/rates",
                        "layer": "primary_source",
                        "observation_as_of": START.date(),
                        "available_at": START - timedelta(minutes=4),
                    }
                ],
            }
        ),
        expected_head=None,
    )
    selected = selection(
        now=START,
        tickers=("1234",),
        scope="specified_stocks",
        runs=runs,
        rows=[stock_analysis("1234", metrics={"market_price_yen": quote, "er_annual": 0.13})],
    )
    prepared = prepare_selected_workspace(
        draft=draft_for(selected),
        db_path=app,
        workspace=tmp_path / "workspace",
        sqlite_path=market,
        runs_path=runs,
        indicators_path=tmp_path / "absent-indicators",
        company_growth_mirror=tmp_path / "mirror",
        now=START,
    )
    operations = OperationService(app)
    saved = selection_binding(operations.get(prepared.operation_id).payload)
    assert saved is not None
    case = SimpleNamespace(
        app=app,
        market=market,
        runs=runs,
        ledger=ledger,
        selected=saved,
        prepared=prepared,
        operations=operations,
        scenario=scenario,
        quote=quote,
    )
    case.document, case.review = _assessment_payload(case)
    directory = prepared.workspace / "1234"
    directory.mkdir(exist_ok=True)
    for name, payload in (
        ("assessment-draft.yaml", case.document),
        ("assessment-review.yaml", case.review),
        ("assessment-publication.yaml", {"assessment_id": assessment_id(), "supersedes_id": None}),
    ):
        (directory / name).write_text(yaml.safe_dump(payload, allow_unicode=True))
    return case


def _assessment_payload(case):
    document, review = pair_payload()
    fixed = case.selected.inputs.securities_context["1234"]
    technical = fixed["technical_analysis"]
    growth = fixed["company_growth"]
    trace = f"{case.prepared.operation_id}; selection_sha256={selection_sha256(case.selected)}"
    sources = document["input_snapshot"]["sources"]
    sources.extend(
        [
            {
                "source_id": "growth-fixed",
                "ticker": "1234",
                "source_tier": "local_data",
                "provider": "jquants",
                "dataset": "fin_summaries",
                "as_of": START.date().isoformat(),
                "retrieved_at": START.isoformat(),
                "used_for": trace
                + "; inputs.securities_context.1234.company_growth; "
                + canonical_json(
                    {
                        key: growth[key]
                        for key in ("status", "reason", "input_release", "read_semantics")
                    }
                ),
            },
            {
                "source_id": "technical-fixed",
                "ticker": "1234",
                "source_tier": "local_data",
                "provider": "jquants",
                "dataset": "daily_bars",
                "as_of": technical["history_end_date"],
                "retrieved_at": START.isoformat(),
                "used_for": trace
                + "; inputs.securities_context.1234.technical_analysis; "
                + canonical_json(
                    {
                        key: technical[key]
                        for key in (
                            "calculation_revision",
                            "price_basis",
                            "history_start_date",
                            "history_end_date",
                            "bar_count",
                            "read_semantics",
                        )
                    }
                )
                + "; seed_end_date="
                + technical["rsi_14bar"]["seed_end_date"],
            },
            {
                "source_id": "market-fixed",
                "ticker": "1234",
                "source_tier": "primary",
                "ref": "https://example.invalid/rates",
                "as_of": "2026-10-14",
                "retrieved_at": START.isoformat(),
                "used_for": trace + f"; inputs.market_context; {CONTEXT_ID}; 金利の個社伝達",
            },
        ]
    )
    external = fixed["external_assessment"]
    observations = (
        [external["minkabu"]["latest"]]
        if case.scenario == "gaps"
        else [item["inputs"]["latest"] for item in external["model_valuations"]]
    )
    external_refs = []
    for observation in observations:
        source_id = "external-" + observation["distributor"]
        external_refs.append(source_id)
        sources.append(
            {
                "source_id": source_id,
                "ticker": "1234",
                "source_tier": "secondary",
                "ref": observation["source_url"],
                "as_of": observation["snapshot_date"],
                "retrieved_at": observation["fetched_at_utc"],
                "used_for": trace
                + "; inputs.securities_context.1234.external_assessment; "
                + canonical_json(
                    {
                        key: observation.get(key)
                        for key in (
                            "origin",
                            "consensus_target",
                            "consensus_target_period",
                            "share_basis",
                            "availability",
                            "target_price_yen",
                        )
                    }
                ),
            }
        )
    document["input_snapshot"]["facts"][0]["value"] = case.quote
    hypotheses = [
        {
            "statement": "過去の増収だけで将来利益成長や割安を保証できるか",
            "origin": "research",
            "result": "unresolved" if case.scenario == "gaps" else "refuted",
            "reason": "企業成長の原本系列が不足し、ゼロ成長や中立と解釈しない。"
            if case.scenario == "gaps"
            else "同じ第1四半期の売上100→120に対し営業利益20→18、利益率20%→15%。"
            "増収と収益性悪化を分け、これだけで将来利益や終価を上積みしない。",
            "source_ids": ["growth-fixed"],
        },
        {
            "statement": "継続収益は価値を支えるか",
            "origin": "research",
            "result": "supported",
            "reason": "合成一次資料で継続収益と固定負債を確認。",
            "source_ids": ["annual"],
        },
        {
            "statement": "金利上昇が直ちに全利益を失わせるか",
            "origin": "research",
            "result": "refuted",
            "reason": "金利逆風はあるが固定負債と手元資金で短期影響は限定。",
            "source_ids": ["annual", "market-fixed"],
        },
        {
            "statement": "外部提示を自己終価へ採用できるか",
            "origin": "research",
            "result": "unresolved" if case.scenario == "gaps" else "refuted",
            "reason": "明示対象外で目標値は欠測。0や中立にしない。"
            if case.scenario == "gaps"
            else "二媒体の同じifis目標1500円は一原評価。自己仮定の裏取りとせず採用しない。",
            "source_ids": external_refs,
        },
        {
            "statement": "価格の位置・勢いを企業価値や反転保証にできるか",
            "origin": "research",
            "result": "refuted",
            "reason": "MA・BB・RSI・騰落率は同じ価格の加工で独立票にしない。"
            + (
                "上側超過・RSI100も価値毀損への反証ではない。"
                if case.scenario == "broken"
                else "下側超過・RSI0も割安や大底を保証しない。"
            ),
            "source_ids": ["technical-fixed"],
        },
    ]
    findings = document["findings"]
    findings.update(hypotheses=hypotheses, source_ids=[source["source_id"] for source in sources])
    findings["value_gap_explanation"] = (
        "価値差と弱い価格局面を分け、Pmax1000円と現金維持を比較する。"
    )
    findings["unresolved_questions"] = ["反転時期は不明。借換条件・収益源が悪化すれば再評価する。"]
    if case.scenario == "gaps":
        findings["unresolved_questions"].append(
            "企業成長の系列・SMA200・外部評価が不足。成長改善を投資理由には採用しない。"
        )
    if case.scenario == "broken":
        document["investment_case"].update(
            status="broken", status_reason="一次資料で恒常収益源の喪失を確認。"
        )
        document["conclusion"]["stance"] = "unsupported"
        hypotheses[1].update(result="refuted", reason="合成一次資料で恒常収益源が失われた。")
        for risk in document["permanent_loss_risks"]:
            if risk["axis"] == "structural_decline":
                risk.update(assessment="adverse", summary="合成一次資料で恒常収益源の喪失を確認。")
        findings["value_gap_explanation"] = "価格上昇・古い外部目標は恒常収益源の喪失を覆さない。"
    if case.scenario == "unknown":
        document["valuation"] = dict(
            status="unresolved",
            market_price_fact_id=None,
            horizon_months=None,
            required_annual_return_pct=None,
            base=None,
            downside=None,
            unresolved_reason="重要な借換条件が未確認で将来価値を確定できない。",
        )
        document["investment_case"].update(status="uncertain", status_reason="借換条件の確認待ち。")
        document["conclusion"]["stance"] = "uncertain"
        hypotheses[2].update(
            result="unresolved", reason="変動負債の借換条件が不明で金利伝達を確定できない。"
        )
        hypotheses[1]["reason"] = "継続収益は確認したが、借換条件は未確認。"
        for risk in document["permanent_loss_risks"]:
            if risk["axis"] == "debt_repayment":
                risk.update(
                    assessment="unknown",
                    evidence_status="unverified",
                    summary="借換の確約条件が未確認で、返済可能性は未確定。",
                )
        findings["value_gap_explanation"] = (
            "借換条件が重要な不足で価値差は未確定。価格指標では補えない。"
        )
        findings["unresolved_questions"] = ["借換の金額・期限・確約条件を一次資料で確認する。"]
    document["conclusion"]["strongest_countercase"] = (
        "価格反転でなく、収益源の喪失や借換不能が価値仮説を覆す。"
    )
    review["checked_source_ids"] = findings["source_ids"]
    review["review_scope"] = (
        "合成の独立役payload。原資料、固定Selectionのbasis/時点/窓/seed、"
        "企業成長の増収と利益率低下、同原外部評価と価格情報の重複、重要欠測を確認する想定。"
        "意味の独立Review成功ではない。"
    )
    review["strongest_countercase"] = document["conclusion"]["strongest_countercase"]
    # 手書きの独立入力golden。作者のProjectionをコピーして検算とはしない。
    review["recalculated_projections"] = (
        []
        if case.scenario == "unknown"
        else [
            {
                "name": "base",
                "terminal_value_per_share_yen": "1100",
                "cash_distribution_per_share_yen": "0",
            },
            {
                "name": "downside",
                "terminal_value_per_share_yen": "700",
                "cash_distribution_per_share_yen": "0",
            },
        ]
    )
    update_review_hash(document, review)
    return (
        StockAssessmentDocument.model_validate(document).model_dump(mode="json"),
        AssessmentReview.model_validate(review).model_dump(mode="json"),
    )


def _promote_and_propose(case):
    promote(workspace=case.prepared.workspace, ticker="1234", db_path=case.app, now=PROMOTED)
    context = build_proposal_context(
        operation_id=case.prepared.operation_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=PREPARED,
    )
    raw = scaffold_proposal(
        operation_id=case.prepared.operation_id,
        proposal_id=PROPOSAL_ID,
        db_path=case.app,
        sqlite_path=case.market,
        now=PREPARED,
    )
    raw.update(
        headline="合成五領域の比較",
        comparison="保存評価の五領域・反証を現金維持と比較した。",
        forgone="価格指標や同原外部評価の票数では購入しない。",
        published_at=PUBLISHED.isoformat(),
    )
    item = raw["items"][0]
    item["review_conditions"] = ["借換不能・恒常収益源の喪失なら価格と独立して再調査する。"]
    if case.scenario == "unknown":
        item.update(
            rationale="借換条件の重要欠測で保留。安い指値でも不足を迂回しない。",
            blockers=[{"code": "valuation_unresolved", "message": "借換条件が未確認"}],
            resume_conditions=["借換条件を確認し、新Assessmentと独立Reviewを作る。"],
        )
    else:
        item.pop("blockers")
        item.pop("resume_conditions")
        if case.scenario == "broken":
            item.update(
                action="no_action",
                rationale="企業固有の収益源喪失を確認。強い価格局面でも買わない。",
            )
        else:
            limit = 900 if case.scenario == "current" else 1000
            item.update(
                action="buy",
                timing="at_current_limit" if case.scenario == "current" else "wait_limit",
                rationale="価格反転は不明だが価値仮説を維持。"
                + (
                    "900円で要求率を満たすため現在指値。"
                    if case.scenario == "current"
                    else "1100円では要求率未達。価値を維持する条件で1000円指値を待つ。"
                ),
                terms=dict(
                    side="buy",
                    limit_price_yen=str(limit),
                    quantity=300,
                    notional_yen=str(limit * 300),
                    target_session="2026-10-15",
                    valuation_as_of="2026-10-14",
                    maximum_entry_price_yen="1000",
                    price_grid_rule_id="tse-common-conservative-20261014",
                    conditions=["仮説・株式単位・cash・注文状態の重要変化がないこと。"],
                    warnings=[],
                ),
            )
    raw["review"].update(
        reviewer_identity="synthetic-independent-proposal-reviewer",
        reviewed_at="2026-10-14T16:15:00+09:00",
    )
    return context, bind_review(
        raw, {assessment_id(): context.assessments[assessment_id()].document}
    )


@pytest.mark.parametrize("scenario", ["wait", "current", "broken", "gaps", "unknown"])
def test_five_domain_assessment_review_action_and_readback(tmp_path, monkeypatch, scenario):
    case = _prepare_case(tmp_path, monkeypatch, scenario)
    projection = project_research_inputs(case.selected)
    security = projection["securities"]["1234"]
    assert security["machine_valuation"]["estimate"]["market_price_yen"] == case.quote
    assert (
        projection["market_context"]["saved_market_context_assessment"]["context_id"] == CONTEXT_ID
    )
    growth = security["company_growth"]
    assert growth["read_semantics"] == "retained_latest_in_fixed_release_not_point_in_time"
    if scenario == "gaps":
        assert growth["status"] == "unavailable"
        assert growth["reason"]
        assert growth["reported"]["rows"] == []
    else:
        assert growth["status"] == "available"
        assert growth["input_release"]["release_id"] == "five-domain-fixed-input"
        assert [row["values"][:2] for row in growth["reported"]["rows"]] == [[100, 20], [120, 18]]
        columns = growth["reported_yoy"]["columns"]
        changes = {
            row["metric"]: row
            for values in growth["reported_yoy"]["rows"]
            if (row := dict(zip(columns, values, strict=True)))["to_end"] == "2026-06-30"
        }
        assert changes["sales"]["rate"] == pytest.approx(0.2)
        assert changes["operating_profit"]["rate"] == pytest.approx(-0.1)
        assert changes["operating_margin"]["margin_points"] == pytest.approx(-5)
    technical = security["technical_analysis"]
    assert technical["rsi_14bar"]["value"] == (100 if scenario == "broken" else 0)
    assert technical["bollinger_20bar"]["position"] == (
        "above_upper" if scenario == "broken" else "below_lower"
    )
    if scenario == "gaps":
        assert technical["moving_averages"]["sma_200bar"]["status"] == "insufficient_history"
        assert technical["moving_averages"]["sma_20bar"]["status"] == "available"
        assert security["external_assessment"]["minkabu"]["latest"]["target_price_yen"] is None
        assert security["external_assessment"]["minkabu"]["latest"]["availability"] == "not_covered"
    context, proposal = _promote_and_propose(case)
    pair = context.assessments[assessment_id()]
    authored = context.payload()["assessments"][assessment_id()]
    assert authored["document"] == case.document
    assert authored["review"] == case.review
    assert set(pair.review.checked_source_ids) == {
        source.source_id for source in pair.document.input_snapshot.sources
    }
    assert context.research_scope.selection_sha256 == selection_sha256(case.selected)
    expected = "no_action" if scenario == "broken" else "defer" if scenario == "unknown" else "buy"
    assert proposal.items[0].action == expected
    assert proposal.items[0].assessment_ref.core_sha256 == pair.core_sha256
    assert proposal.items[0].assessment_ref.review_id == pair.review.review_id
    if expected == "buy":
        assert proposal.items[0].terms.maximum_entry_price_yen == Decimal(1000)
        assert proposal.items[0].terms.quantity == 300
        assert proposal.items[0].terms.limit_price_yen == Decimal(
            900 if scenario == "current" else 1000
        )
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(proposal).valid
    published = service.publish(proposal)
    assert get_investment_proposal(proposal_id=PROPOSAL_ID, db_path=case.app) == published
    assert LedgerStoreService(case.app).load() == case.ledger
    before = (case.prepared.workspace / "domain-inputs.yaml").read_bytes()
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close=800")
    save_observations(
        case.market, _external_observations(PUBLISHED + timedelta(minutes=1), target=600)
    )
    changed_external = external_assessment(
        case.market,
        ticker="1234",
        cutoff=PUBLISHED + timedelta(minutes=2),
        evaluated_at=PUBLISHED + timedelta(minutes=2),
    )
    assert {item.inputs.latest.consensus_target for item in changed_external.model_valuations} == {
        600
    }
    case.runs.unlink()
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.workspace.selected_prepare.collect_research_inputs",
        lambda **_kw: pytest.fail("resume recollected mutable inputs"),
    )
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.inputs.company_growth",
        lambda *_a, **_kw: pytest.fail("resume recalculated Company Growth"),
    )
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.inputs.technical_analysis",
        lambda *_a, **_kw: pytest.fail("resume recalculated Technical Analysis"),
    )
    (case.prepared.workspace / "domain-inputs.yaml").unlink()
    resume_selected_workspace(
        operation_id=case.prepared.operation_id,
        db_path=case.app,
        workspace=case.prepared.workspace,
        now=PUBLISHED,
        force=True,
    )
    assert (case.prepared.workspace / "domain-inputs.yaml").read_bytes() == before
    assert (
        project_research_inputs(
            selection_binding(case.operations.get(case.prepared.operation_id).payload)
        )
        == projection
    )
    with closing(connect_read_only(case.app)) as connection:
        assert load_reviewed_assessment(connection, assessment_id()) == pair
    preview = preview_current_terms(
        proposal_id=PROPOSAL_ID,
        db_path=case.app,
        sqlite_path=case.market,
        now=PUBLISHED + timedelta(minutes=2),
    )
    assert preview.current_inputs.quotes[0].price_yen == Decimal(800)
    assert get_investment_proposal(proposal_id=PROPOSAL_ID, db_path=case.app) == published


@pytest.mark.parametrize("change", ["source_basis", "hypothesis_reason", "countercase"])
def test_changed_evidence_or_reason_cannot_promote_with_original_review(
    tmp_path, monkeypatch, change
):
    case = _prepare_case(tmp_path, monkeypatch)
    changed = deepcopy(case.document)
    if change == "source_basis":
        source = next(
            source
            for source in changed["input_snapshot"]["sources"]
            if source["source_id"] == "technical-fixed"
        )
        source["used_for"] += "; basisの再確認で採用根拠が変わった。"
    elif change == "hypothesis_reason":
        changed["findings"]["hypotheses"][1]["reason"] = "変動負債への伝達を追加確認した。"
    else:
        changed["conclusion"]["strongest_countercase"] = "新しい最強反証が判明した。"
    assert (
        assessment_core_hash(StockAssessmentDocument.model_validate(changed))
        != case.review["reviewed_assessment_sha256"]
    )
    # 元Reviewを保持する。update_review_hashで拒否条件を消してはならない。
    (case.prepared.workspace / "1234/assessment-draft.yaml").write_text(yaml.safe_dump(changed))
    with pytest.raises(ResearchWorkspaceDataError, match=r"stale|hash"):
        promote(workspace=case.prepared.workspace, ticker="1234", db_path=case.app, now=PROMOTED)
    with closing(connect_read_only(case.app)) as connection:
        assert connection.execute("SELECT count(*) FROM stock_assessment").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM stock_assessment_review").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["rationale", "comparison", "review_conditions"])
def test_changed_action_reason_cannot_publish_with_original_content_review(
    tmp_path, monkeypatch, change
):
    case = _prepare_case(tmp_path, monkeypatch)
    _, proposal = _promote_and_propose(case)
    raw = proposal.payload()
    if change == "comparison":
        raw["comparison"] = "金利の重要な伝達経路を改めて採用した比較理由。"
    elif change == "rationale":
        raw["items"][0]["rationale"] = "待つ経済的理由を変更した。"
    else:
        raw["items"][0]["review_conditions"].append("新しい借換条件を再確認する。")
    changed = TradeProposal.model_validate(raw)
    # 初回の正常draft以降はbind_reviewを呼ばず、元Reviewをそのまま渡す。
    assert changed.review == proposal.review
    assert proposal_draft_sha256(changed) != changed.review.draft_sha256
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    assert service.check(changed, require_review=False).valid
    with pytest.raises(ProposalConflictError, match="content review"):
        service.publish(changed)
    with closing(connect_read_only(case.app)) as connection:
        for table in (
            "investment_proposal",
            "investment_proposal_item",
            "position_decision_reference",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
