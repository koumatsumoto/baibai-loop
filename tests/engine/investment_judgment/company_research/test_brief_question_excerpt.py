"""問いの省略を、原本の金額・条件・旧固定入力の変更にしない。"""

import pytest
from tests.engine.investment_judgment.company_research.test_brief_evidence import NOW
from tests.engine.investment_judgment.company_research.test_brief_unresolved_omissions import _save
from tests.helpers.generated_selection import seed_generated_selection

from baibai_engine.investment_judgment.company_research.context import company_context_brief
from baibai_engine.investment_judgment.company_research.query import research_catalog
from baibai_engine.investment_judgment.workflow.selection import parse_selection, selection_sha256
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)


@pytest.mark.parametrize("kind", ["research", "assessment"])
@pytest.mark.parametrize("length", [239, 240, 241])
def test_question_excerpt_marks_only_actual_truncation_within_existing_limit(
    tmp_path, kind, length
):
    question = "問" * length
    path = tmp_path / "app"
    context = _save(path, kind, [question])
    original = context.model_dump_json()
    before = path.read_bytes()
    brief = research_catalog(path, as_of=NOW.date(), known_at=NOW)["items"][0][kind]
    expected = question if length <= 240 else question[:239] + "…"
    assert brief["unresolved_questions"] == [expected]
    assert len(expected) <= 240
    assert "unresolved_questions_omitted" not in brief
    assert "exact reference" in brief["display"]
    assert context.model_dump_json() == original
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["research", "assessment"])
def test_question_excerpt_keeps_unicode_codepoints_and_marks_partial_number(tmp_path, kind):
    unicode_question = "株" * 238 + "😀末尾の限定"
    numeric_question = "条件" * 117 + "終価=1234.567円、未入金の分配は別に加算しない"
    context = _save(tmp_path / "app", kind, [unicode_question, numeric_question])
    original = context.model_dump_json()
    brief = company_context_brief(context)[kind]
    assert brief["unresolved_questions"] == ["株" * 238 + "😀…", numeric_question[:239] + "…"]
    assert all(len(text) == 240 for text in brief["unresolved_questions"])
    assert "exact reference" in brief["display"]
    brief["unresolved_questions"].clear()
    assert context.model_dump_json() == original


@pytest.mark.parametrize("kind", ["research", "assessment"])
def test_display_excerpt_does_not_rewrite_fixed_selection_or_original_question(tmp_path, kind):
    question = "確認" * 130 + "元の数値や未解決の条件は原本で読む"
    app, runs = tmp_path / "app", tmp_path / "runs"
    context = _save(app, kind, [question, "利益", "資金", "権利"])
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
    brief = result["saved_company_context"]["1234"][kind]
    assert brief == company_context_brief(context)[kind]
    assert brief["unresolved_questions"] == [question[:239] + "…", "利益", "資金"]
    assert brief["unresolved_questions_omitted"] == 1
    assert result["candidate_tickers"] == []
    parsed = parse_selection(selected.model_dump(mode="json"))
    assert parsed.model_dump_json() == original
    assert selection_sha256(parsed) == digest
    assert (app.read_bytes(), runs.read_bytes()) == before
