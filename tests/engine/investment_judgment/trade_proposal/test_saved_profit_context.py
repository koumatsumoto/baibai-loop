"""同じ固定入力の利益・期・出典を併読し、訂正や価値の自動採択にしない。"""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.db_seed import seed_ledger
from tests.helpers.model_generation import publish_generation
from tests.helpers.research_selection import selected_target

from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
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
    GenerationMetadata,
    GenerationSubject,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.generation.financial_models import (
    generate_financial_and_price_models,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.screening.policies.technical_financial import (
    TechnicalFinancialPolicy,
)
from baibai_engine.machine_analysis.screening.policies.technical_financial_selection import (
    evaluate_technical_financial_rows,
    project_technical_financial_row,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument

AS_OF = date(2026, 8, 31)
EVALUATED = datetime(2026, 9, 2, tzinfo=UTC)


def _models(profit=-80.0, *, original_profit=24.0):
    def document(year, period, end, sales, op, parent, disclosed):
        return JQuantsFinancialSummary(
            ticker="1001",
            disclosed_at=date.fromisoformat(disclosed),
            disclosed_time="15:30:00",
            disclosure_number=f"synthetic-{year}-{period}",
            document_type=f"{period}FinancialStatements_Consolidated_JP",
            fiscal_period=period,
            fiscal_year_end=date(year + 1, 3, 31),
            period_start=date(year, 4, 1),
            period_end=date.fromisoformat(end),
            sales=sales,
            operating_profit=op,
            profit=parent,
            equity=1000.0,
            shares_outstanding=100.0,
            treasury_shares=0.0,
        )

    original = document(2026, "1Q", "2026-06-30", 150.0, 30.0, original_profit, "2026-08-10")
    documents = (
        document(2024, "1Q", "2024-06-30", 100.0, 10.0, 8.0, "2024-08-10"),
        document(2024, "FY", "2025-03-31", 400.0, 40.0, 32.0, "2025-05-10"),
        document(2025, "1Q", "2025-06-30", 100.0, 10.0, 8.0, "2025-08-10"),
        document(2025, "FY", "2026-03-31", 500.0, 50.0, 40.0, "2026-05-10"),
        original,
        replace(
            original,
            disclosed_at=date(2026, 9, 1),
            disclosure_number="synthetic-later-correction",
            profit=profit,
        ),
    )
    return generate_financial_and_price_models(
        ticker="1001",
        documents=documents,
        adjusted_bars=(PriceBar(AS_OF, 10.0, None),),
        adjustment_events=(),
        trading_dates=(AS_OF,),
        expected_price_date=AS_OF,
        sector="情報・通信業",
        as_of=AS_OF,
        input_release=InputRelease(release_id="synthetic-fixed", manifest_sha256="a" * 64),
        ttm_rules=DEFAULT_TTM_RULES,
    )


def _prepare(tmp_path, models, *, screening=True):
    market = GeneratedModel(
        model_id="market_context",
        subject="1001",
        calculation_revision="synthetic-sector",
        payload={"master": {"sector_33": "情報・通信業"}},
    )
    models = (*models, market)
    generation = ModelGeneration(
        schema_version=2,
        generation_id="synthetic-fixed-profits",
        as_of=AS_OF,
        evaluation_time=EVALUATED,
        inputs={
            "synthetic": InputSnapshot(kind="lake_release", reference="fixed", sha256="a" * 64)
        },
        subject_plan={
            "1001": GenerationSubject(
                scope="stock",
                model_ids=(
                    "company_financials",
                    "company_growth",
                    "technical_analysis",
                    "market_context",
                ),
            )
        },
        models=models,
    )
    app, runs = tmp_path / "app.sqlite", tmp_path / "runs.sqlite"
    publish_generation(generation, path=runs)
    selection_id = None
    if screening:
        metadata = GenerationMetadata.model_validate(generation.model_dump(exclude={"models"}))
        row = project_technical_financial_row("1001", models=models, metadata=metadata)
        result = evaluate_technical_financial_rows(
            metadata,
            tickers=("1001",),
            rows={"1001": row},
            policy=TechnicalFinancialPolicy(),
        )
        selection_id = "synthetic-selection"
        ScreeningSelectionStore(runs).publish(selection_id, result)
    now = max(datetime.now(UTC), EVALUATED) + timedelta(seconds=1)
    seed_ledger(
        app,
        PortfolioLedgerDocument.model_validate(
            {
                "schema_version": 3,
                "portfolio_scope": "repository_only",
                "as_of": now,
                "events": [
                    {
                        "type": "opening_balance",
                        "event_id": "synthetic",
                        "occurred_at": now,
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
                text="合成：利益の時点差を確認",
                scope="specified_stocks",
                requested_tickers=("1001",),
            ),
            research_as_of=AS_OF,
            input_known_at=now,
            generation_id=generation.generation_id,
            screening_selection_id=selection_id,
            selection_summary="G/Fを独立の買付票にしない",
            targets=(selected_target("1001"),),
        ),
        db_path=app,
        runs_path=runs,
        workspace=tmp_path / "workspace",
        now=now,
    )
    operation = OperationService(app).record_unfinished(
        prepared.operation_id,
        ticker="1001",
        reason="利益の採用を未確認",
        references=("synthetic-fixed",),
        now=now + timedelta(seconds=1),
    )
    saved = selection_binding(operation.payload)
    runs.rename(tmp_path / "retained-runs.sqlite")
    context = build_proposal_context(
        operation_id=operation.operation_id,
        db_path=app,
        sqlite_path=tmp_path / "absent-market.sqlite",
        now=now + timedelta(seconds=2),
    )
    return context, saved, app


def _row(context):
    return context.payload()["saved_profit_context"]["rows"][0]


def test_final_context_retains_different_profit_vintages_without_changing_discovery(tmp_path):
    context, saved, app = _prepare(tmp_path, _models())
    before = app.read_bytes(), saved.model_dump_json()
    result = context.payload()["saved_profit_context"]
    row = result["rows"][0]
    assert result["selection_sha256"] == selection_sha256(saved)
    assert result["generation_id"] == saved.inputs.generation.generation_id
    assert result["generation_as_of"] == AS_OF.isoformat()
    assert result["evaluation_time"] == EVALUATED.isoformat()
    assert result["input_known_at"] == saved.input_known_at.isoformat()
    assert row["comparison"] == {"state": "different_recorded_numbers", "reasons": []}
    assert row["company_financials"]["observation"]["profit_ttm_yen"] == 56.0
    assert row["company_growth"]["observation"]["parent_profit"]["value"] == -48.0
    assert (
        row["company_financials"]["period"]
        == row["company_growth"]["period"]
        == ["2025-07-01", "2026-06-30"]
    )
    assert (
        row["company_growth"]["read_semantics"]
        == "retained_latest_in_fixed_release_not_point_in_time"
    )
    assert row["company_financials"]["read_semantics"] != row["company_growth"]["read_semantics"]
    assert any(
        d["disclosed_at"] == "2026-09-01"
        for d in row["company_growth"]["observation"]["parent_profit"]["documents"]
    )
    assert all(
        d["disclosed_at"] <= "2026-08-31"
        for d in row["company_financials"]["observation"]["profit_components"]
    )
    discovery = context.payload()["saved_discovery_context"]["rows"][0]
    assert discovery["financial_gate"]["state"] == "research_candidate"
    assert "earning_power" in discovery["financial_paths"]["matched_routes"]
    assert context.payload()["assessments"] == context.payload()["calculations"] == {}
    row["company_growth"]["observation"]["parent_profit"]["documents"].clear()
    assert _row(context)["company_growth"]["observation"]["parent_profit"]["documents"]
    assert (app.read_bytes(), saved.model_dump_json()) == before


def test_equal_numbers_do_not_assert_same_source_or_correction(tmp_path):
    context, _, _ = _prepare(tmp_path, _models(24.0))
    row = _row(context)
    assert row["comparison"] == {"state": "same_recorded_number", "reasons": []}
    assert row["company_financials"]["observation"]["profit_ttm_yen"] == 56.0
    assert row["company_growth"]["observation"]["parent_profit"]["value"] == 56.0
    assert (
        row["company_financials"]["observation"]["profit_components"][0]["disclosed_at"]
        == "2026-08-10"
    )
    assert (
        row["company_growth"]["observation"]["parent_profit"]["documents"][0]["disclosed_at"]
        == "2026-09-01"
    )
    assert "corrected_source" not in row["comparison"]


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("period", "period_mismatch"),
        ("basis", "statement_basis_mismatch"),
        ("standard", "accounting_standard_mismatch"),
        ("unit", "unit_mismatch"),
        ("missing_profit", "profit_unavailable"),
        ("missing_period", "period_unavailable"),
        ("unavailable_reason", "profit_unavailable"),
        ("unavailable_period", "growth_period_unavailable"),
    ],
)
def test_unaligned_or_missing_observations_are_not_a_numeric_difference(tmp_path, mode, reason):
    models = list(_models())
    growth = deepcopy(models[1].payload)
    observation = next(
        o for o in growth["periods"][-1]["observations"] if o["measurement"] == "annual_ttm"
    )
    if mode == "period":
        observation["period_start"] = "2025-07-02"
    elif mode == "basis":
        observation["statement_basis"] = "nonconsolidated"
    elif mode == "standard":
        observation["accounting_standard"] = "IFRS"
    elif mode == "unit":
        growth["metric_units"]["parent_profit"] = "million_JPY"
    elif mode == "missing_profit":
        observation["metrics"]["parent_profit"] = {
            "value": None,
            "reason": "synthetic_missing",
            "documents": [],
        }
    elif mode == "unavailable_reason":
        observation["metrics"]["parent_profit"]["reason"] = "synthetic_unverified"
    elif mode == "unavailable_period":
        observation["period_status"] = "accounting_periods_incomparable"
    else:
        cf = deepcopy(models[0].payload)
        cf["earnings_price"]["profit_components"] = []
        models[0] = models[0].model_copy(update={"payload": cf})
    models[1] = models[1].model_copy(update={"payload": growth})
    context, _, _ = _prepare(tmp_path, tuple(models), screening=False)
    assert _row(context)["comparison"]["state"] == "not_comparable"
    assert reason in _row(context)["comparison"]["reasons"]


def test_missing_growth_model_remains_not_generated(tmp_path):
    models = _models()
    context, _, _ = _prepare(tmp_path, (models[0], models[2]), screening=False)
    row = _row(context)
    assert row["company_growth"]["status"] == "not_generated"
    assert row["comparison"]["state"] == "not_comparable"
    assert row["company_financials"]["observation"]["profit_ttm_yen"] == 56.0


def test_legacy_partial_earnings_contract_is_not_replaced_by_growth_number(tmp_path):
    models = list(_models())
    cf = deepcopy(models[0].payload)
    cf["earnings_price"] = {"profit_ttm_yen": 56.0}
    models[0] = models[0].model_copy(update={"payload": cf})
    context, _, _ = _prepare(tmp_path, tuple(models), screening=False)
    row = _row(context)
    assert row["company_financials"]["observation"] == {"profit_ttm_yen": 56.0}
    assert row["company_financials"]["period"] == [None, None]
    assert row["comparison"]["state"] == "not_comparable"


def test_unknown_statement_basis_does_not_become_agreement(tmp_path):
    models = list(_models(24.0))
    cf = deepcopy(models[0].payload)
    cf["earnings_price"]["accounting_standard"] = None
    models[0] = models[0].model_copy(update={"payload": cf})
    context, _, _ = _prepare(tmp_path, tuple(models), screening=False)
    assert "accounting_standard_unavailable" in _row(context)["comparison"]["reasons"]


def test_zero_profit_and_precision_are_retained_without_normalization(tmp_path):
    models = list(_models(-32.0, original_profit=-32.0))
    growth = deepcopy(models[1].payload)
    annual = next(
        o for o in growth["periods"][-1]["observations"] if o["measurement"] == "annual_ttm"
    )
    value = annual["metrics"]["parent_profit"]
    assert value["value"] == models[0].payload["earnings_price"]["profit_ttm_yen"] == 0.0
    value["precision"] = "derived_from_truncated_display_amounts"
    models[1] = models[1].model_copy(update={"payload": growth})
    context, saved, _ = _prepare(tmp_path, tuple(models), screening=False)
    row = _row(context)
    assert row["comparison"]["state"] == "same_recorded_number"
    assert row["company_growth"]["observation"]["parent_profit"] == value
    assert row["company_financials"]["full_result_ref"] == "/inputs/generation/models/0/payload"
    assert (
        row["company_growth"]["calculation_revision"]
        == saved.inputs.generation.result("company_growth", "1001").calculation_revision
    )


def test_old_selection_without_generation_stays_unrecorded():
    from tests.helpers.research_selection import selection

    from baibai_engine.investment_judgment.workflow.profit_context import saved_profit_context

    legacy = selection(now=EVALUATED, tickers=("1001",), scope="specified_stocks")
    before = legacy.model_dump_json()
    assert saved_profit_context(legacy, selection_hash=selection_sha256(legacy)) is None
    assert legacy.model_dump_json() == before


@pytest.mark.parametrize("value", [10**400, -(10**400), 2**53 + 1])
def test_large_saved_integer_profit_does_not_stop_proposal_context(tmp_path, value):
    models = list(_models())
    cf = deepcopy(models[0].payload)
    cf["earnings_price"]["profit_ttm_yen"] = value
    models[0] = models[0].model_copy(update={"payload": cf})
    context, saved, app = _prepare(tmp_path, tuple(models), screening=False)
    before = app.read_bytes(), selection_sha256(saved)
    row = _row(context)
    amount = row["company_financials"]["observation"]["profit_ttm_yen"]
    assert type(amount) is int
    assert amount == value
    # Keeping a raw amount does not make its period/basis valid or select a valuation.
    assert row["comparison"]["state"] != "same_recorded_number"
    assert context.payload()["assessments"] == context.payload()["calculations"] == {}
    assert (app.read_bytes(), selection_sha256(saved)) == before


@pytest.mark.parametrize(
    "value", [None, True, False, "1", float("inf"), float("-inf"), float("nan")]
)
def test_profit_comparison_does_not_coerce_invalid_numbers(value):
    from baibai_engine.investment_judgment.workflow.profit_context import _number

    assert _number(value) is None
