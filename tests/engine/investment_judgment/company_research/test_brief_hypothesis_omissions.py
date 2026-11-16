"""先頭だけの再訪表示を、未表示の反証がないことに置き換えない。"""

import pytest
from tests.engine.investment_judgment.company_research.test_brief_evidence import (
    NOW,
    _hypothesis,
    _save,
    _source,
)
from tests.helpers.generated_selection import seed_generated_selection

from baibai_engine.investment_judgment.company_research.context import company_context_brief
from baibai_engine.investment_judgment.company_research.document import ResearchDraft
from baibai_engine.investment_judgment.company_research.store import ResearchStoreService
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)


def _hypotheses(results):
    return [
        _hypothesis(f"合成仮説{index}", ["same-source"], result=result)
        for index, result in enumerate(results)
    ]


@pytest.mark.parametrize("kind", ["research", "assessment"])
def test_later_refutation_and_unknown_are_excerpts_in_original_relative_order(tmp_path, kind):
    path = tmp_path / "app"
    hypotheses = _hypotheses(["supported"] * 3 + ["refuted", "unresolved"])
    context = _save(path, kind, [_source("same-source")], hypotheses)
    original = context.model_dump_json()
    before = path.read_bytes()
    brief = company_context_brief(context)[kind]
    assert [row["statement"] for row in brief["hypotheses"]] == [
        item["statement"] for item in (hypotheses[0], hypotheses[3], hypotheses[4])
    ]
    assert [row["result"] for row in brief["hypotheses"]] == ["supported", "refuted", "unresolved"]
    assert brief["hypotheses_omitted"] == 2
    assert brief["omitted_hypothesis_results"] == ["supported"]
    assert "exact reference" in brief["display"]
    assert brief["role"] == (
        "unreviewed_research"
        if kind == "research"
        else "historical_reviewed_assessment_not_current_readiness"
    )
    brief["omitted_hypothesis_results"].clear()
    brief["hypotheses"][0]["result"] = "refuted"
    assert context.model_dump_json() == original
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["research", "assessment"])
def test_omission_result_kinds_are_bounded_not_independent_votes(tmp_path, kind):
    results = ["supported"] * 3 + ["refuted"] * 20 + ["unresolved", "supported"]
    context = _save(tmp_path / "app", kind, [_source("same-source")], _hypotheses(results))
    brief = company_context_brief(context)[kind]
    assert len(brief["hypotheses"]) == 3
    assert brief["hypotheses_omitted"] == 22
    assert brief["omitted_hypothesis_results"] == ["refuted", "supported"]
    assert "hypothesis_score" not in brief
    assert "result_counts" not in brief


@pytest.mark.parametrize("kind", ["research", "assessment"])
@pytest.mark.parametrize("count", [1, 3])
def test_untruncated_old_record_does_not_get_synthetic_omission_fields(tmp_path, kind, count):
    context = _save(
        tmp_path / "app", kind, [_source("same-source")], _hypotheses(["unresolved"] * count)
    )
    brief = company_context_brief(context)[kind]
    assert len(brief["hypotheses"]) == count
    assert "hypotheses_omitted" not in brief
    assert "omitted_hypothesis_results" not in brief


def test_partial_research_without_hypotheses_keeps_unknown_not_empty_clearance(tmp_path):
    draft = ResearchDraft.model_validate(
        {
            "research_id": "questions-only",
            "document": {
                "ticker": "1234",
                "as_of": NOW.date().isoformat(),
                "investigated_scope": "合成の未取得理由だけ",
                "unresolved_questions": ["採算を確認していない"],
            },
        }
    )
    path = tmp_path / "app"
    from baibai_engine.investment_judgment.company_research.query import read_company_contexts

    saved = ResearchStoreService(path, clock=lambda: NOW).save(draft)
    context = read_company_contexts(path, tickers=("1234",), as_of=NOW.date(), known_at=NOW)
    brief = company_context_brief(context["1234"])["research"]
    assert brief["hypotheses"] == []
    assert brief["unresolved_questions"] == ["採算を確認していない"]
    assert "hypotheses_omitted" not in brief
    assert "omitted_hypothesis_results" not in brief
    assert ResearchStoreService(path, clock=lambda: NOW).save(draft) == saved


def test_same_selection_context_exposes_omission_without_changing_candidates_or_sources(tmp_path):
    app, runs = tmp_path / "app", tmp_path / "runs"
    context = _save(
        app,
        "research",
        [_source("same-source")],
        _hypotheses(["supported"] * 3 + ["refuted"]),
    )
    selection = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=app)
    before = app.read_bytes(), runs.read_bytes()
    selected = load_generated_selection_context(
        request=selection.request,
        generation_id=selection.inputs.generation.generation_id,
        screening_selection_id=selection.inputs.screening_selection_id,
        research_as_of=NOW.date(),
        known_at=NOW,
        runs_path=runs,
        db_path=app,
        comparison_tickers=("1234",),
    )
    brief = selected["saved_company_context"]["1234"]["research"]
    assert brief == company_context_brief(context)["research"]
    assert brief["hypotheses_omitted"] == 1
    assert brief["omitted_hypothesis_results"] == ["supported"]
    assert selected["candidate_tickers"] == []
    assert (app.read_bytes(), runs.read_bytes()) == before


@pytest.mark.parametrize(
    ("results", "indices"),
    [
        (["unresolved", "unresolved", "unresolved", "refuted", "supported"], [0, 3, 4]),
        (["refuted", "refuted", "supported", "refuted"], [0, 1, 2]),
        (["supported"] * 5, [0, 1, 2]),
        (["refuted", "supported", "unresolved"], [0, 1, 2]),
    ],
)
@pytest.mark.parametrize("kind", ["research", "assessment"])
def test_result_coverage_does_not_prioritize_a_result_or_count_votes(
    tmp_path, kind, results, indices
):
    hypotheses = _hypotheses(results)
    context = _save(tmp_path / "app", kind, [_source("same-source")], hypotheses)
    brief = company_context_brief(context)[kind]
    assert [row["statement"] for row in brief["hypotheses"]] == [
        hypotheses[i]["statement"] for i in indices
    ]
    assert {row["result"] for row in brief["hypotheses"]} == set(results)
    assert brief.get("hypotheses_omitted", 0) == len(results) - len(indices)
