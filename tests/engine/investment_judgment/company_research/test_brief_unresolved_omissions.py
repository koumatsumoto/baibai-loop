"""有限の再訪表示で、未表示の問いを解決済み・存在しないことにしない。"""

import pytest
from tests.engine.investment_judgment.company_research.test_brief_evidence import NOW
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.generated_selection import seed_generated_selection

from baibai_engine.investment_judgment.company_research.context import company_context_brief
from baibai_engine.investment_judgment.company_research.document import ResearchDraft
from baibai_engine.investment_judgment.company_research.query import (
    read_company_contexts,
    research_catalog,
)
from baibai_engine.investment_judgment.company_research.store import ResearchStoreService
from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.workflow.selection import parse_selection, selection_sha256
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)


def _save(path, kind, questions):
    if kind == "research":
        ResearchStoreService(path, clock=lambda: NOW).save(
            ResearchDraft.model_validate(
                {
                    "research_id": "question-omission",
                    "document": {
                        "ticker": "1234",
                        "as_of": NOW.date().isoformat(),
                        "investigated_scope": "未確認の問いを残す合成研究",
                        "unresolved_questions": questions,
                        "hypotheses": [
                            {
                                "statement": "未調査の仮説",
                                "origin": "research",
                                "result": "unresolved",
                                "reason": "今回の合成入力では未調査",
                                "source_ids": [],
                            }
                        ],
                    },
                }
            )
        )
    else:
        document, review = pair_payload()
        document["findings"]["unresolved_questions"] = questions
        update_review_hash(document, review)
        AssessmentStoreService(path, clock=lambda: NOW).publish_reviewed_assessment(
            assessment_id(), document, review
        )
    return read_company_contexts(path, tickers=("1234",), as_of=NOW.date(), known_at=NOW)["1234"]


@pytest.mark.parametrize("kind", ["research", "assessment"])
def test_unshown_questions_are_counted_without_changing_saved_text_order_or_stance(tmp_path, kind):
    questions = ["長い問い" * 80, "利益の持続性", "投資後cash", "資本異動の確認", "権利の確認"]
    path = tmp_path / "app"
    context = _save(path, kind, questions)
    original = context.model_dump_json()
    before = path.read_bytes()
    brief = research_catalog(path, as_of=NOW.date(), known_at=NOW)["items"][0][kind]
    assert brief["unresolved_questions"] == [questions[0][:239] + "…", *questions[1:3]]
    assert brief["unresolved_questions_omitted"] == 2
    assert "exact reference" in brief["display"]
    assert brief["role"] == (
        "unreviewed_research"
        if kind == "research"
        else "historical_reviewed_assessment_not_current_readiness"
    )
    if kind == "assessment":
        assert brief["stance"] == context.assessment.stance
    brief["unresolved_questions"].clear()
    brief["unresolved_questions_omitted"] = 0
    assert context.model_dump_json() == original
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["research", "assessment"])
@pytest.mark.parametrize("count", [0, 1, 3])
def test_complete_short_question_list_keeps_the_old_brief_shape(tmp_path, kind, count):
    questions = [f"合成の問い{n}" for n in range(count)]
    context = _save(tmp_path / "app", kind, questions)
    brief = company_context_brief(context)[kind]
    assert brief["unresolved_questions"] == questions
    assert "unresolved_questions_omitted" not in brief


def test_selection_context_reports_omission_without_changing_fixed_inputs_or_candidate_set(
    tmp_path,
):
    app, runs = tmp_path / "app", tmp_path / "runs"
    context = _save(app, "research", ["利益", "cash", "価値", "資本異動を確認"])
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=app)
    original = selected.model_dump_json()
    digest = selection_sha256(selected)
    before = app.read_bytes(), runs.read_bytes()
    result = load_generated_selection_context(
        request=selected.request,
        generation_id=selected.inputs.generation.generation_id,
        screening_selection_id=selected.inputs.screening_selection_id,
        research_as_of=NOW.date(),
        known_at=NOW,
        runs_path=runs,
        db_path=app,
        comparison_tickers=("1234",),
    )
    brief = result["saved_company_context"]["1234"]["research"]
    assert brief == company_context_brief(context)["research"]
    assert brief["unresolved_questions_omitted"] == 1
    assert result["candidate_tickers"] == []
    parsed = parse_selection(selected.model_dump(mode="json"))
    assert parsed.model_dump_json() == original
    assert selection_sha256(parsed) == digest
    assert (app.read_bytes(), runs.read_bytes()) == before
