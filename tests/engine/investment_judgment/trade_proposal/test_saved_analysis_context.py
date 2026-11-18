"""固定L2の留保を最終比較へ戻す。外部見解や共有状態を採択に昇格させない。"""

from datetime import timedelta

import pytest
from tests.helpers.db_seed import seed_historical_operation, seed_ledger
from tests.helpers.model_generation import publish_generation
from tests.helpers.proposal import PREPARED
from tests.helpers.research_selection import historical_operation, selected_target, selection

from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.workflow.generated_inputs import project_generated_inputs
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.workspace import prepare_selected_workspace
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationSubject,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.generation.query import read_research_model_inputs
from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument

START = PREPARED - timedelta(minutes=3)


def _seed(tmp_path, *, planned=True, external=True, technical=None, market=None):
    payloads = [
        ("company_financials", "1234", {"known_loss": -10}),
        ("market_context", "market", {"value": None, "reason": "not_observed"}),
    ]
    if technical is not None:
        payloads.append(("technical_analysis", "1234", technical))
    if market is not None:
        payloads.append(("market_context", "1234", market))
    if external:
        payloads.append(
            (
                "external_assessment",
                "1234",
                {
                    "origin_groups": {"IFIS": ["monex", "tradingview"]},
                    "comparisons": [{"value": 100, "reason": "unknown_share_basis"}],
                    "available_at": (START - timedelta(days=1)).isoformat(),
                },
            )
        )
    if planned:
        payloads.extend(
            [
                ("industry_trends", "industry:test", {"coverage": "extraction_pending"}),
                ("social_trends", "market:global", {"observations": [], "coverage": "partial"}),
            ]
        )
    plan = {
        "1234": GenerationSubject(
            scope="stock",
            model_ids=(
                "company_financials",
                "company_growth",
                "technical_analysis",
                "external_assessment",
                "market_context",
            ),
            related_subjects=("industry:test",),
            related_subject_basis={"industry:test": "合成分類。全社感応度ではない"},
        ),
        "industry:test": GenerationSubject(scope="sector", model_ids=("industry_trends",)),
        "market": GenerationSubject(scope="market", model_ids=("market_context",)),
        "market:global": GenerationSubject(scope="market", model_ids=("social_trends",)),
    }
    generation = ModelGeneration(
        schema_version=2 if planned else 1,
        generation_id="synthetic-fixed-environment",
        as_of=START.date(),
        evaluation_time=START - timedelta(minutes=1),
        inputs={
            "market": InputSnapshot(
                kind="lake_release", reference="synthetic-fixed", sha256="a" * 64
            )
        },
        subject_plan=plan if planned else None,
        models=tuple(
            GeneratedModel(
                model_id=model, subject=subject, calculation_revision="retained-v1", payload=payload
            )
            for model, subject, payload in payloads
        ),
    )
    runs = tmp_path / "runs.sqlite"
    publish_generation(generation, path=runs)
    return runs, generation


@pytest.mark.parametrize("planned", [False, True])
@pytest.mark.parametrize("external", [False, True])
def test_environment_view_separates_display_omission_from_not_generated(
    tmp_path, planned, external
):
    runs, generation = _seed(
        tmp_path, planned=planned, external=external, technical={"rsi_14bar": None}
    )
    saved = read_research_model_inputs(generation.generation_id, targets=("1234",), path=runs)
    before = saved.model_dump_json()
    complete = project_generated_inputs(saved)
    view = project_generated_inputs(saved, proposal_view=True)
    row = view["securities"]["1234"]
    assert row["not_generated"] == complete["securities"]["1234"]["not_generated"]
    assert ("external_assessment" in row["not_generated"]) is not external
    assert set(row["omitted_from_view"]) == {"company_financials"}
    models = row["models"] if planned else row
    assert "company_financials" not in models
    # 古い生成にモメンタムがなければ「0」「横ばい」や別の価格指標で埋めない。
    index = next(i for i, m in enumerate(saved.models) if m.model_id == "technical_analysis")
    assert models["technical_analysis"] == {
        "full_result_ref": f"/inputs/generation/models/{index}/payload"
    }
    if external:
        assert models["external_assessment"] == saved.result("external_assessment", "1234").payload
        models["external_assessment"]["comparisons"].clear()
    if planned:
        assert view["shared_subjects"] == {
            subject: {**value, "omitted_from_view": []}
            for subject, value in complete["shared_subjects"].items()
        }
    else:
        assert view["market_context"] == complete["market_context"]
    assert view["analysis_source"] == complete["analysis_source"]
    assert saved.model_dump_json() == before
    assert project_generated_inputs(saved) == complete


@pytest.mark.parametrize(
    ("peer_start", "peer_end", "alignment"),
    [
        ("2026-09-10", "2026-10-13", "same_recorded_endpoints"),
        ("2026-09-09", "2026-10-13", "different_recorded_endpoints"),
        ("2026-09-10", "2026-10-09", "different_recorded_endpoints"),
        (None, "2026-10-13", "endpoints_unavailable"),
        ("bad-date", "2026-10-13", "endpoints_unavailable"),
        ("2026-10-14", "2026-10-13", "endpoints_unavailable"),
    ],
)
def test_proposal_compares_recorded_r20_dates_not_returns(
    tmp_path, peer_start, peer_end, alignment
):
    market = {
        "price_path_relative": {
            "benchmark_ticker": "1321",
            "comparisons": {
                "R20": {
                    "comparison_start_date": "2026-09-10",
                    "comparison_end_date": "2026-10-13",
                    "own_return": -0.08,
                    "benchmark_return": None,
                    "relative_return": None,
                    "status": "missing_benchmark_endpoint",
                }
            },
        },
        "relative": {
            "sector": {
                "comparison_start_date": peer_start,
                "comparison_end_date": peer_end,
                "peer_count": 1,
                "relative_return_20d": 0.01,
            }
        },
    }
    runs, generation = _seed(tmp_path, market=market)
    saved = read_research_model_inputs(generation.generation_id, targets=("1234",), path=runs)
    subject = project_generated_inputs(saved, proposal_view=True)["securities"]["1234"]
    assert subject["r20_index_sector_windows"] == {
        "index_r20_window": ["2026-09-10", "2026-10-13"],
        "sector_20bar_window": None
        if alignment == "endpoints_unavailable"
        else [peer_start, peer_end],
        "alignment": alignment,
    }
    # 同一日付は代理指数価格の存在や比較成立の認定ではない。旧保存・通常調査の表示は不変。
    assert subject["models"]["market_context"] == market
    assert "r20_index_sector_windows" not in project_generated_inputs(saved)["securities"]["1234"]


@pytest.mark.parametrize("momentum_status", ["available", "missing_endpoint", "not_generated"])
def test_final_context_keeps_saved_environment_without_source_or_latest_head(
    tmp_path, monkeypatch, momentum_status
):
    as_of = START.date()
    technical = {
        "as_of": as_of.isoformat(),
        "history_end_date": (as_of - timedelta(days=2)).isoformat(),
        "price_basis": "price_adjusted_close",
        "read_semantics": "retained_prices_at_date_cutoff_not_original_publication_vintage",
        "price_momentum_revision": "common-calendar-252-21-v2",
        "price_momentum": {
            "reference_date": as_of.isoformat(),
            "start_date": (as_of - timedelta(days=365)).isoformat(),
            "end_date": (as_of - timedelta(days=30)).isoformat(),
            "status": momentum_status,
            "value": -0.12 if momentum_status == "available" else None,
        },
        "price_path": {"freshness": "stale"},
        "rsi_14bar": {"rsi": 67},
    }
    runs, generation = _seed(
        tmp_path, technical=None if momentum_status == "not_generated" else technical
    )
    app, market = tmp_path / "app.sqlite", tmp_path / "absent-market.sqlite"
    seed_ledger(
        app,
        PortfolioLedgerDocument.model_validate(
            {
                "schema_version": 3,
                "portfolio_scope": "repository_only",
                "as_of": START,
                "events": [
                    {
                        "type": "opening_balance",
                        "event_id": "synthetic",
                        "occurred_at": START,
                        "amount_yen": 1,
                    }
                ],
            }
        ),
    )
    prepared = prepare_selected_workspace(
        draft=GeneratedResearchSelectionDraft(
            schema_version=2,
            request=ResearchRequest(
                text="合成：留保を照合", scope="specified_stocks", requested_tickers=("1234",)
            ),
            research_as_of=START.date(),
            input_known_at=START,
            generation_id=generation.generation_id,
            selection_summary="合成：外部見解を採択せず調べる",
            targets=(selected_target(),),
        ),
        db_path=app,
        runs_path=runs,
        workspace=tmp_path / "workspace",
        now=START,
    )
    operation = OperationService(app).record_unfinished(
        prepared.operation_id,
        ticker="1234",
        reason="合成：外部比較の留保を未確認",
        references=("synthetic-fixed",),
        now=PREPARED,
    )
    saved = selection_binding(operation.payload)
    digest = selection_sha256(saved)
    runs.rename(tmp_path / "retained-runs.sqlite")

    def forbidden(*_args, **_kwargs):
        pytest.fail("final comparison must not reread live model inputs")

    monkeypatch.setattr(
        "baibai_engine.machine_analysis.generation.query.read_research_model_inputs", forbidden
    )
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.market_assessment.query.latest_assessment", forbidden
    )
    before = app.read_bytes()
    context = build_proposal_context(
        operation_id=operation.operation_id, db_path=app, sqlite_path=market, now=PREPARED
    )
    payload = context.payload()
    assert "saved_analysis_context" in payload
    expected = project_generated_inputs(saved.inputs.generation, proposal_view=True)
    assert payload["saved_analysis_context"] == expected
    row = payload["saved_analysis_context"]["securities"]["1234"]
    if momentum_status == "not_generated":
        assert "technical_analysis" in row["not_generated"]
        assert "technical_analysis" not in row["models"]
    else:
        shown = row["models"]["technical_analysis"]
        assert {k: v for k, v in shown.items() if k != "full_result_ref"} == {
            k: v for k, v in technical.items() if k not in {"price_path", "rsi_14bar"}
        }
        full = saved.model_dump(mode="json")
        for key in shown["full_result_ref"].strip("/").split("/"):
            full = full[int(key)] if isinstance(full, list) else full[key]
        assert full == technical
        assert "technical_analysis" not in row["not_generated"]
        shown["price_momentum"]["value"] = 42
        assert context.payload()["saved_analysis_context"] == expected
    assert payload["saved_market_context_assessment"] is None
    assert payload["assessments"] == {}
    payload["saved_analysis_context"]["shared_subjects"].clear()
    assert context.payload()["saved_analysis_context"] == expected
    assert (
        selection_sha256(
            selection_binding(OperationService(app).get(operation.operation_id).payload)
        )
        == digest
    )
    assert app.read_bytes() == before


def test_retained_pre_generation_selection_is_not_filled_with_current_models(tmp_path):
    app = tmp_path / "app.sqlite"
    legacy = selection(now=START, tickers=("1234",), scope="specified_stocks")
    operation = historical_operation(legacy)
    seed_historical_operation(app, operation)
    seed_ledger(
        app,
        PortfolioLedgerDocument.model_validate(
            {
                "schema_version": 3,
                "portfolio_scope": "repository_only",
                "as_of": START,
                "events": [
                    {
                        "type": "opening_balance",
                        "event_id": "synthetic",
                        "occurred_at": START,
                        "amount_yen": 1,
                    }
                ],
            }
        ),
    )
    OperationService(app).record_unfinished(
        operation.operation_id,
        ticker="1234",
        reason="旧保存の未完",
        references=("legacy",),
        now=PREPARED,
    )
    before = app.read_bytes()
    payload = build_proposal_context(
        operation_id=operation.operation_id,
        db_path=app,
        sqlite_path=tmp_path / "absent",
        now=PREPARED,
    ).payload()
    assert payload["saved_analysis_context"] is None
    assert selection_sha256(
        selection_binding(OperationService(app).get(operation.operation_id).payload)
    ) == selection_sha256(legacy)
    assert app.read_bytes() == before
