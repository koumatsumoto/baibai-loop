"""新政策の有限カード・共有文脈・明示詳細と、固定入力からの再開。"""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
import yaml
from pydantic import TypeAdapter
from tests.helpers.model_generation import seed_generation
from tests.helpers.research_selection import selected_target

from baibai_engine.investment_judgment.workflow.inputs import project_research_inputs
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
    parse_selection,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)
from baibai_engine.investment_judgment.workflow.workspace.selected_prepare import (
    prepare_selected_workspace,
    resume_selected_workspace,
)
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationSubject,
    ModelGeneration,
)
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.models.company_growth.models import (
    CompanyGrowth,
    Observation,
    PeriodState,
    Value,
)
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.policies.economic_paths import (
    CompanyCoordinates,
    EconomicPathPolicy,
    PathCoordinates,
    select_economic_paths,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore

NOW = datetime(2026, 10, 25, 9, tzinfo=UTC)
AS_OF = date(2026, 10, 23)


def seed_economic_generation(path, *, scoped=True):
    original = seed_generation(path, now=NOW, as_of=AS_OF)
    stock_ids = tuple(model.model_id for model in original.models if model.subject == "1234")
    models = [model for model in original.models if model.subject == "market"]
    for ticker in ("1234", "5678", "9999"):
        for model in original.models:
            if model.subject != "1234":
                continue
            payload = model.payload
            if model.model_id == "company_growth":
                payload = CompanyGrowth(
                    ticker=ticker,
                    status="available",
                    requested_start=date(2005, 1, 1),
                    requested_end=AS_OF,
                    periods=tuple(
                        PeriodState(
                            fiscal_period="FY",
                            fiscal_year_end=date(year, 12, 31),
                            period_start=date(year, 1, 1),
                            period_end=date(year, 12, 31),
                            observations=(
                                Observation(
                                    period_start=date(year, 1, 1),
                                    period_end=date(year, 12, 31),
                                    measurement="annual_ttm",
                                    statement_basis="consolidated",
                                    metrics={"sales": Value(value=float(year))},
                                ),
                                Observation(
                                    period_start=date(year, 10, 1),
                                    period_end=date(year, 12, 31),
                                    measurement="quarter",
                                    statement_basis="consolidated",
                                    metrics={"sales": Value(value=year / 10)},
                                ),
                            ),
                        )
                        for year in range(2005, 2025)
                    ),
                ).model_dump(mode="json")
            models.append(
                GeneratedModel(
                    model_id=model.model_id,
                    subject=ticker,
                    calculation_revision=model.calculation_revision,
                    payload=payload,
                )
            )
        for model_id in ("stress_probe", "catalyst_probe"):
            models.append(
                GeneratedModel(
                    model_id=model_id,
                    subject=ticker,
                    calculation_revision="probe-v1",
                    payload={"ticker": ticker, "value": None, "reason": "not_observed"},
                )
            )
    plan = {
        ticker: GenerationSubject(
            scope="stock",
            model_ids=(*stock_ids, "stress_probe", "catalyst_probe"),
            related_subjects=("sector:food" if ticker != "9999" else "sector:other",),
        )
        for ticker in ("1234", "5678", "9999")
    }
    plan["market"] = GenerationSubject(scope="market", model_ids=("market_context",))
    for subject in ("sector:food", "sector:funding", "sector:other", "sector:unrelated"):
        plan[subject] = GenerationSubject(
            scope="sector",
            model_ids=("sector_probe",),
            related_subjects=("sector:funding",) if subject == "sector:food" else (),
        )
        models.append(
            GeneratedModel(
                model_id="sector_probe",
                subject=subject,
                calculation_revision="probe-v1",
                payload={"subject": subject, "value": -0.1},
            )
        )
    generation = ModelGeneration.model_validate(
        {
            **original.model_dump(),
            "generation_id": "economic-fixed",
            "schema_version": 2 if scoped else 1,
            **({"subject_plan": plan} if scoped else {}),
            "models": models,
        }
    )
    ModelGenerationStore(path).publish(generation)
    evidence = {
        "operating_observation": {
            "measurement": "quarter",
            "period_start": "2026-07-01",
            "period_end": "2026-09-30",
            "statement_basis": "consolidated",
            "metric": "operating_profit",
            "observation": {"value": -40.0, "reason": None, "documents": [{"id": "source-1"}]},
        },
        "operating_change": {
            "from_value": -70.0,
            "to_value": -40.0,
            "difference": 30.0,
            "reason": "nonpositive_base",
            "measurement": "quarter",
        },
        "book_provenance": "individual_book_source_scope_and_measurement_date_not_exposed",
    }
    card_context = {
        "financial": {
            "fcf_ttm": -20.0,
            "cash": 100.0,
            "debt": 90.0,
            "edinet_source_doc_id": "doc-1",
            "tradable_share_change_yoy": 0.05,
            "earnings_forecast_source": {
                "status": "available",
                "fiscal_year_end": "2027-03-31",
                "statement_basis": "consolidated",
                "disclosed_date": "2026-10-20",
            },
        },
        "growth_comparison_basis": "source_reported_economic_scope_unverified",
        "latest_growth_changes": {"operating_profit": evidence["operating_change"]},
        "price_momentum": {
            "value": None,
            "status": "unavailable",
            "reason": "missing_endpoint",
            "start_date": "2025-09-30",
            "end_date": "2026-09-30",
            "reference_date": "2026-10-23",
        },
        "external_assessment": {
            "comparisons": [
                {
                    "metric": "target_price",
                    "state": "first_observation",
                    "comparable": False,
                    "delta": None,
                    "origin": "same-origin",
                    "target_period": "FY2027",
                },
                {
                    "metric": "earnings",
                    "state": "increased",
                    "comparable": True,
                    "delta": 10.0,
                    "origin": "same-origin",
                    "target_period": "FY2027",
                },
                *(
                    {
                        "metric": "earnings",
                        "state": state,
                        "comparable": False,
                        "delta": None,
                        "origin": "same-origin",
                        "target_period": "FY2027",
                    }
                    for state in ("recovered", "count_changed", "not_comparable")
                ),
            ]
        },
        "full_result_reference": {"generation_id": generation.generation_id},
    }
    result = select_economic_paths(
        tuple(
            CompanyCoordinates(
                ticker,
                ticker,
                "food",
                (
                    PathCoordinates(
                        "operating_recovery",
                        "quarter",
                        0.1 if ticker == "1234" else 0.2,
                        0.03 if ticker == "1234" else 0.06,
                        "food",
                        "food",
                        True,
                        evidence,
                    ),
                ),
                card_context,
                dict.fromkeys((*stock_ids, "stress_probe", "catalyst_probe"), "probe-v1"),
                (),
            )
            for ticker in ("1234", "5678")
        ),
        policy=EconomicPathPolicy(),
    )
    screening = ScreeningSelection(generation.generation_id, ("1234", "5678", "9999"), result)
    ScreeningSelectionStore(path).publish("economic-policy", screening)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE screening_selection SET published_at=? WHERE selection_id=?",
            ((NOW - timedelta(seconds=30)).isoformat(), "economic-policy"),
        )
    return generation, screening


def context(path, *, request=None, comparison_tickers=(), db_path=None):
    return load_generated_selection_context(
        request=request or ResearchRequest(text="調査する", scope="discover_new"),
        generation_id=None,
        screening_selection_id="economic-policy",
        research_as_of=AS_OF,
        known_at=NOW,
        runs_path=path,
        db_path=db_path or path.parent / "absent",
        comparison_tickers=comparison_tickers,
    )


@pytest.mark.parametrize("scoped", [False, True])
def test_initial_cards_keep_evidence_without_reading_candidate_details(
    tmp_path, monkeypatch, scoped
):
    from baibai_engine.machine_analysis.generation import query

    runs = tmp_path / "runs"
    _, screening = seed_economic_generation(runs, scoped=scoped)
    actual_read = query.read_model_inputs
    read_subjects = []

    def read(*args, subjects, **kwargs):
        read_subjects.append(set(subjects))
        return actual_read(*args, subjects=subjects, **kwargs)

    monkeypatch.setattr(query, "read_model_inputs", read)
    result = context(runs)
    expected = {"market", "sector:food", "sector:funding"} if scoped else {"market"}
    assert read_subjects == [expected]
    assert result["model_inputs"]["securities"] == {}
    assert result["candidate_tickers"] == ["1234", "5678"]
    assert result["candidate_order_semantics"] == (
        "ticker_code_order_attention_priority_in_screening_cards_rank"
    )
    assert [card["ticker"] for card in result["screening"]["cards"]] == ["5678", "1234"]
    assert [card["rank"] for card in result["screening"]["cards"]] == [1, 2]
    if scoped:
        assert set(result["model_inputs"]["shared_subjects"]) == expected
    assert (
        result["screening"]["cards"]
        == TypeAdapter(type(screening.result)).dump_python(screening.result, mode="json")["cards"]
    )
    card = result["screening"]["cards"][0]
    assert card["paths"][0]["evidence"]["operating_observation"]["observation"]["value"] == -40.0
    assert card["context"]["price_momentum"]["reason"] == "missing_endpoint"
    assert card["context"]["price_momentum"]["end_date"] == "2026-09-30"
    assert (
        card["context"]["financial"]["earnings_forecast_source"]["fiscal_year_end"] == "2027-03-31"
    )
    comparisons = card["context"]["external_assessment"]["comparisons"]
    assert [item["state"] for item in comparisons] == [
        "first_observation",
        "increased",
        "recovered",
        "count_changed",
        "not_comparable",
    ]
    assert [item["delta"] for item in comparisons] == [None, 10.0, None, None, None]
    # 表示を編集しても保存政策の観測やidentityを変更しない。
    card["paths"][0]["evidence"]["operating_observation"]["observation"]["value"] = 999
    assert (
        screening.result.cards[0].paths[0].evidence["operating_observation"]["observation"]["value"]
        == -40.0
    )


@pytest.mark.parametrize("specified", [False, True])
@pytest.mark.parametrize("scoped", [False, True])
def test_explicit_candidate_and_non_candidate_details_keep_all_n_models(
    tmp_path, specified, scoped
):
    runs = tmp_path / "runs"
    generation, _ = seed_economic_generation(runs, scoped=scoped)
    baseline = context(runs)
    request = (
        ResearchRequest(
            text="指定を調査する", scope="specified_stocks", requested_tickers=("9999",)
        )
        if specified
        else None
    )
    result = context(
        runs, request=request, comparison_tickers=("1234",) if specified else ("1234", "9999")
    )
    assert result["screening"] == baseline["screening"]
    assert result["candidate_tickers"] == baseline["candidate_tickers"]
    assert set(result["model_inputs"]["securities"]) == {"1234", "9999"}
    assert "5678" not in result["model_inputs"]["securities"]
    if scoped:
        assert "sector:other" in result["model_inputs"]["shared_subjects"]
    for ticker in ("1234", "9999"):
        details = result["model_inputs"]["securities"][ticker]
        models = details["models"] if scoped else details
        assert {"stress_probe", "catalyst_probe"}.issubset(models)
        assert models["stress_probe"] == generation.result("stress_probe", ticker).payload
        growth = models["company_growth"]
        assert growth == generation.result("company_growth", ticker).payload
        assert len(growth["periods"]) == 20
        assert "full_result_ref" not in growth
        older_observations = growth["periods"][0]["observations"]
        assert older_observations[0]["measurement"] == "annual_ttm"
        assert older_observations[0]["metrics"]["sales"]["value"] == 2005.0
        assert older_observations[1]["measurement"] == "quarter"
        assert older_observations[1]["metrics"]["sales"]["value"] == 200.5
        assert details["not_generated"] == []


def test_held_details_remain_available_without_promoting_them_to_requested(tmp_path, monkeypatch):
    import baibai_engine.investment_judgment.workflow.selection_query as query

    runs = tmp_path / "runs"
    generation, _ = seed_economic_generation(runs)
    # Exercise the held-target boundary independently of ledger replay arithmetic.
    db = tmp_path / "app"
    db.touch()
    monkeypatch.setattr(query, "connect_read_only", lambda path: sqlite3.connect(path))
    monkeypatch.setattr(
        query, "load_ledger_document", lambda connection: SimpleNamespace(events=())
    )
    monkeypatch.setattr(
        query,
        "replay_events_through",
        lambda *args: SimpleNamespace(
            lots={"9999": [SimpleNamespace(quantity=1)], "5678": [SimpleNamespace(quantity=0)]}
        ),
    )
    monkeypatch.setattr(query, "latest_assessment", lambda *args, **kwargs: None)
    result = context(runs, db_path=db)
    assert result["held_tickers"] == ("9999",)
    assert result["requested_tickers"] == ()
    assert result["request"]["requested_tickers"] == []
    assert set(result["model_inputs"]["securities"]) == {"9999"}
    assert result["model_inputs"]["securities"]["9999"]["models"]["company_growth"] == (
        generation.result("company_growth", "9999").payload
    )
    assert "sector:food" in result["model_inputs"]["shared_subjects"]
    assert "sector:other" in result["model_inputs"]["shared_subjects"]


def test_prepare_saves_full_economic_policy_and_chosen_models_then_resumes_without_runs(tmp_path):
    runs = tmp_path / "runs"
    generation, screening = seed_economic_generation(runs)
    draft = GeneratedResearchSelectionDraft(
        schema_version=2,
        request=ResearchRequest(text="比較して調査", scope="discover_new"),
        research_as_of=AS_OF,
        input_known_at=NOW,
        generation_id=generation.generation_id,
        screening_selection_id="economic-policy",
        selection_summary="有限カードから選定",
        targets=(selected_target("1234"),),
    )
    workspace, db = tmp_path / "research", tmp_path / "app.sqlite"
    prepared = prepare_selected_workspace(
        draft=draft,
        db_path=db,
        workspace=workspace,
        runs_path=runs,
        now=NOW,
    )
    saved = selection_binding(OperationService(db).get(prepared.operation_id).payload)
    assert saved.inputs.screening_selection == screening
    assert saved.inputs.generation.result("company_growth", "1234") == generation.result(
        "company_growth", "1234"
    )
    assert saved.inputs.generation.result("catalyst_probe", "1234") == generation.result(
        "catalyst_probe", "1234"
    )
    assert saved.inputs.generation.result("company_growth", "5678") is None
    original = project_research_inputs(saved)
    raw, digest = saved.model_dump(mode="json"), selection_sha256(saved)
    growth_projection = original["securities"]["1234"]["models"]["company_growth"]
    complete = raw
    for segment in growth_projection["full_result_ref"].strip("/").split("/"):
        complete = complete[int(segment)] if isinstance(complete, list) else complete[segment]
    assert complete == generation.result("company_growth", "1234").payload
    runs.unlink()
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=db,
        workspace=workspace,
        now=NOW,
        force=True,
    )
    assert resumed.selection_sha256 == digest
    assert yaml.safe_load((workspace / "domain-inputs.yaml").read_text()) == original
    restored = parse_selection(raw)
    assert restored.model_dump(mode="json") == raw
    assert selection_sha256(restored) == digest
