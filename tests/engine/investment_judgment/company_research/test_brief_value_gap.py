"""再訪で事業説明と価値差の判断を混ぜず、旧根拠を現在価格へ適用しない。"""

from datetime import timedelta

import pytest
from tests.engine.investment_judgment.company_research.test_revisit_countercase import NOW
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.generated_selection import seed_generated_selection

from baibai_engine.investment_judgment.company_research.context import company_context_brief
from baibai_engine.investment_judgment.company_research.query import (
    read_company_contexts,
    research_catalog,
)
from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.workflow.selection import parse_selection, selection_sha256


@pytest.mark.parametrize(
    "explanation", ["税益を含むPERだけで持続的な割安としない。", "条件と根拠。" * 100]
)
def test_saved_value_gap_reaches_revisit_without_price_or_stance_changes(tmp_path, explanation):
    app, runs = tmp_path / "app", tmp_path / "runs"
    document, review = pair_payload()
    document["findings"]["value_gap_explanation"] = explanation
    update_review_hash(document, review)
    pair = AssessmentStoreService(app, clock=lambda: NOW).publish_reviewed_assessment(
        assessment_id(), document, review
    )
    selection = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=app)
    before = (
        app.read_bytes(),
        runs.read_bytes(),
        selection.model_dump_json(),
        selection_sha256(selection),
    )
    brief = research_catalog(app, as_of=NOW.date(), known_at=NOW)["items"][0]["assessment"]
    assert brief["value_gap_explanation"] == explanation[:400]
    assert brief["value_gap_explanation_truncated"] == (len(explanation) > 400)
    assert brief["stance"] == pair.document.conclusion.stance
    assert brief["reference"]["core_sha256"] == pair.core_sha256
    assert brief["role"] == "historical_reviewed_assessment_not_current_readiness"
    assert brief["as_of"] == pair.document.input_snapshot.as_of.isoformat()
    assert brief["published_at"] == pair.published_at.isoformat()
    # A previously saved Selection already has the original findings; only its brief changes.
    parsed = parse_selection(selection.model_dump(mode="json"))
    context = parsed.inputs.saved_company_context["1234"]
    assert (
        company_context_brief(context)["assessment"]["value_gap_explanation"] == explanation[:400]
    )
    assert context.assessment.findings.value_gap_explanation == explanation
    brief["value_gap_explanation"] = "returned copy only"
    assert (
        app.read_bytes(),
        runs.read_bytes(),
        parsed.model_dump_json(),
        selection_sha256(parsed),
    ) == before
    hidden = read_company_contexts(
        app, tickers=("1234",), as_of=NOW.date(), known_at=NOW - timedelta(seconds=1)
    )["1234"]
    assert "value_gap_explanation" not in company_context_brief(hidden)["assessment"]
