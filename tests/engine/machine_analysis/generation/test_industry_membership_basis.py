"""固定した業界の関連理由。曝露の計算や候補政策へ昇格させない。"""

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.engine.machine_analysis.generation.test_generation_assembly import AS_OF, NOW, generate
from tests.helpers.trends import generation_inputs

from baibai_engine.investment_judgment.workflow.generated_inputs import project_generated_inputs
from baibai_engine.machine_analysis.generation.contracts import GenerationSubject
from baibai_engine.machine_analysis.generation.query import read_research_model_inputs
from baibai_engine.machine_analysis.generation.runners import trend_inputs
from baibai_engine.machine_analysis.generation.runners.trend_inputs import TrendGenerationInputs
from baibai_engine.machine_analysis.generation.store import ModelGenerationStore
from baibai_engine.machine_analysis.observations.trends.inputs import TrendCoverage


def read_plans(monkeypatch, plans):
    monkeypatch.setattr(trend_inputs, "connect_read_only", lambda _: sqlite3.connect(":memory:"))
    monkeypatch.setattr(
        trend_inputs,
        "read_trend_source",
        lambda *args, **kwargs: ((), TrendCoverage(state="not_collected")),
    )
    return trend_inputs.read_trend_inputs(
        Path("unused.sqlite"),
        release_id="synthetic",
        release_sha256="a" * 64,
        cutoff=NOW,
        as_of=AS_OF,
        industries=plans,
    )


@pytest.mark.parametrize("basis", [None, "", "A group business; not whole-company exposure"])
def test_reader_preserves_recorded_basis_without_a_required_explanation(monkeypatch, basis):
    plan = trend_inputs.DEFAULT_INDUSTRIES[0].model_copy(update={"membership_basis": basis})
    fixed = read_plans(monkeypatch, (plan,))
    assert fixed.industry_links == ((plan.definition.scope_id, plan.related_tickers),)
    expected = None if basis is None else {plan.definition.scope_id: basis}
    assert fixed.industry_membership_basis == expected
    assert ("industry_membership_basis" in fixed.model_dump()) == (basis is not None)


def test_old_subject_and_trend_input_roundtrip_without_new_fields():
    old_subject = (
        '{"scope":"stock","model_ids":["company_financials"],'
        '"related_subjects":["industry:synthetic"]}'
    )
    restored = GenerationSubject.model_validate_json(old_subject)
    assert restored.related_subject_basis is None
    assert restored.model_dump_json() == old_subject
    old_inputs = generation_inputs().model_dump_json()
    assert '"industry_membership_basis"' not in old_inputs
    assert TrendGenerationInputs.model_validate_json(old_inputs).model_dump_json() == old_inputs


def test_two_industry_reasons_do_not_cross_or_change_model_results(monkeypatch):
    first, second = trend_inputs.DEFAULT_INDUSTRIES
    plans = (
        first.model_copy(update={"related_tickers": ("1001", "1002"), "membership_basis": "A"}),
        second.model_copy(update={"related_tickers": ("1001",), "membership_basis": "B"}),
    )
    fixed = read_plans(monkeypatch, plans)
    tickers = ("1001", "1002", "1003")
    current = generate(tickers=tickers, trends=fixed)
    old = generate(
        tickers=tickers, trends=fixed.model_copy(update={"industry_membership_basis": None})
    )
    expected = {f"industry:{plan.definition.scope_id}": plan.membership_basis for plan in plans}
    assert current.subject_plan["1001"].related_subject_basis == expected
    assert current.subject_plan["1002"].related_subject_basis == {
        f"industry:{first.definition.scope_id}": "A"
    }
    assert current.subject_plan["1003"].related_subject_basis is None
    assert current.models == old.models
    assert current.model_dump(exclude={"subject_plan"}) == old.model_dump(exclude={"subject_plan"})
    for subject, definition in current.subject_plan.items():
        assert (
            definition.model_dump(exclude={"related_subject_basis"})
            == old.subject_plan[subject].model_dump()
        )
        if definition.scope != "stock":
            assert definition.related_subject_basis is None
    fixed.industry_membership_basis[first.definition.scope_id] = "changed after generation"
    assert current.subject_plan["1001"].related_subject_basis == expected


@pytest.mark.parametrize("basis", [None, "fixed original reason"])
def test_saved_generation_reaches_l3_without_using_current_defaults(tmp_path, monkeypatch, basis):
    plan = trend_inputs.DEFAULT_INDUSTRIES[0].model_copy(
        update={"related_tickers": ("1001",), "membership_basis": basis}
    )
    fixed = read_plans(monkeypatch, (plan,))
    original = generate(trends=fixed)
    path = tmp_path / "runs.sqlite"
    ModelGenerationStore(path).publish(original)
    monkeypatch.setattr(
        trend_inputs,
        "DEFAULT_INDUSTRIES",
        (plan.model_copy(update={"membership_basis": "new current explanation"}),),
    )
    saved = read_research_model_inputs(original.generation_id, targets=("1001", "1002"), path=path)
    before = saved.model_dump_json()
    projection = project_generated_inputs(saved)
    subject_plan = projection["analysis_source"]["subject_plan"]
    related = f"industry:{plan.definition.scope_id}"
    assert subject_plan["1001"]["related_subjects"] == [related]
    if basis is None:
        assert "related_subject_basis" not in subject_plan["1001"]
    else:
        assert subject_plan["1001"]["related_subject_basis"] == {related: basis}
    assert "related_subject_basis" not in subject_plan["1002"]
    assert saved.model_dump_json() == before
    path.unlink()
    assert project_generated_inputs(saved) == projection


def test_optional_metadata_cannot_create_an_unrelated_subject():
    with pytest.raises(ValidationError, match="basis must refer to a related subject"):
        GenerationSubject(
            scope="stock", model_ids=(), related_subject_basis={"industry:unrelated": "reason"}
        )
    with pytest.raises(ValidationError, match="basis must refer to a linked industry scope"):
        TrendGenerationInputs.model_validate(
            generation_inputs().model_dump()
            | {"industry_membership_basis": {"unrelated": "reason"}}
        )
