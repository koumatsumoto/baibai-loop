"""保存前contextの省略参照はOperation内の未作成pathを指さない。"""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from tests.helpers.model_generation import publish_generation
from tests.helpers.retained_model_generation import seed_generation

from baibai_engine.investment_judgment.workflow.selection import ResearchRequest
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)
from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationSubject,
    ModelGeneration,
)
from baibai_engine.machine_analysis.generation.query import read_model_inputs
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore

NOW = datetime(2026, 11, 12, 9, tzinfo=UTC)
AS_OF = date(2026, 11, 11)


def _saved_generation(runs, schema_version):
    original = seed_generation(runs, now=NOW, as_of=AS_OF)
    semantics = {"known_at_applied": False, "point_in_time_guaranteed": False}
    market = {
        "reading": {
            "read_semantics": semantics,
            "series": [
                {
                    "series_id": "synthetic",
                    "observed_at": "2026-10-31",
                    "latest_value": 0,
                    "read_semantics": {**semantics, "vintage_cutoff": None},
                }
            ],
        }
    }
    models = (
        *(
            model.model_copy(update={"payload": market}) if model.subject == "market" else model
            for model in original.models
        ),
        # The full generation index differs from its Research slice index.
        GeneratedModel(
            model_id="market_context",
            subject="0001",
            calculation_revision="synthetic-v1",
            payload={"unselected_stock": True},
        ),
    )
    plan = {
        subject: GenerationSubject(
            scope="market" if subject == "market" else "stock",
            model_ids=tuple(model.model_id for model in models if model.subject == subject),
        )
        for subject in ("0001", "1234", "market")
    }
    generation = ModelGeneration.model_validate(
        {
            **original.model_dump(),
            "generation_id": "readable-shared-synthetic",
            "schema_version": schema_version,
            "subject_plan": plan if schema_version == 2 else None,
            "models": models,
        }
    )
    publish_generation(generation, path=runs)
    selections = ScreeningSelectionStore(runs)
    selections.publish(
        "context-policy",
        replace(selections.read("policy-1"), generation_id=generation.generation_id),
    )
    with closing(sqlite3.connect(runs)) as connection, connection:
        connection.execute("UPDATE screening_selection SET published_at=?", (NOW.isoformat(),))
    return generation


@pytest.mark.parametrize("schema_version", [1, 2])
@pytest.mark.parametrize("scope", ["specified_stocks", "discover_new"])
def test_selection_context_references_read_the_same_fixed_models(tmp_path, schema_version, scope):
    runs = tmp_path / "runs.sqlite"
    generation = _saved_generation(runs, schema_version)
    context = load_generated_selection_context(
        request=ResearchRequest(
            text="固定入力の鮮度を確認",
            scope=scope,
            requested_tickers=("1234",) if scope == "specified_stocks" else (),
        ),
        generation_id=generation.generation_id,
        screening_selection_id="context-policy",
        research_as_of=AS_OF,
        known_at=NOW,
        runs_path=runs,
        db_path=tmp_path / "absent.sqlite",
    )
    inputs = context["model_inputs"]
    projected = (
        inputs["market_context"]
        if schema_version == 1
        else inputs["shared_subjects"]["market"]["models"]["market_context"]
    )
    assert "full_result_ref" not in projected  # There is no saved Selection in this context.
    reference = projected["generation_model_ref"]
    assert reference == {
        "generation_id": generation.generation_id,
        "subject": "market",
        "model_id": "market_context",
    }
    exact = read_model_inputs(
        reference["generation_id"], subjects=(reference["subject"],), path=runs
    ).result(reference["model_id"], reference["subject"])
    assert exact == generation.result("market_context", "market")
    reading = projected["reading"]
    reconstructed = {
        **reading["read_semantics"],
        **reading["series"][0]["read_semantics"],
    }
    assert reconstructed == exact.payload["reading"]["series"][0]["read_semantics"]
    assert projected["reading"]["series"][0]["observed_at"] == "2026-10-31"
    growth = inputs["securities"]["1234"]
    if schema_version == 2:
        growth = growth["models"]
    growth = growth["company_growth"]
    assert "full_result_ref" not in growth
    if scope == "discover_new":
        reference = growth["generation_model_ref"]
        assert reference["model_id"] == "company_growth"
        exact_growth = read_model_inputs(
            reference["generation_id"], subjects=(reference["subject"],), path=runs
        ).result(reference["model_id"], reference["subject"])
        assert exact_growth == generation.result("company_growth", "1234")
    else:
        assert growth == generation.result("company_growth", "1234").payload
