"""再訪の選定材料に、当時の価値実現時期と待機の両側のリスクを残す。"""

from contextlib import closing
from datetime import datetime, timedelta

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.generated_selection import seed_generated_selection

from baibai_engine.investment_judgment.company_research.context import company_context_brief
from baibai_engine.investment_judgment.company_research.query import (
    read_company_contexts,
    research_catalog,
)
from baibai_engine.investment_judgment.stock_assessment.store import (
    AssessmentStoreService,
    load_reviewed_assessment,
)
from baibai_engine.investment_judgment.workflow.selection import selection_sha256
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


def _save(path, realization):
    document, review = pair_payload()
    document["findings"]["realization"] = realization
    # A timing-only question need not be duplicated in hypothesis/question prose.
    document["findings"]["hypotheses"] = []
    document["findings"]["unresolved_questions"] = []
    update_review_hash(document, review)
    pair = AssessmentStoreService(path, clock=lambda: NOW).publish_reviewed_assessment(
        assessment_id(), document, review
    )
    context = read_company_contexts(path, tickers=("1234",), as_of=NOW.date(), known_at=NOW)["1234"]
    return pair, context


@pytest.mark.parametrize("timing", ["known", "uncertain"])
def test_catalog_and_selection_brief_keep_historical_realization_without_retiming(tmp_path, timing):
    app, runs = tmp_path / "app", tmp_path / "runs"
    realization = {
        "mechanism": "業界需要と価格転嫁が継続利益・株主cashへ届く仮説。",
        "timing": timing,
        "timing_reason": "次の契約更新が再観測の節目。会社目標期を株価回復期限にしない。",
        "delay_risk": "需要回復の遅延は借換負担を増やす一方、回復が早ければ待機で機会を失う。",
    }
    pair, context = _save(app, realization)
    original = context.model_dump_json()
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=app)
    selection_digest = selection_sha256(selected)
    before = app.read_bytes(), runs.read_bytes()

    catalog = research_catalog(app, as_of=NOW.date(), known_at=NOW)
    brief = catalog["items"][0]["assessment"]
    assert brief["realization"] == realization
    assert brief["role"] == "historical_reviewed_assessment_not_current_readiness"
    assert brief["as_of"] == pair.document.input_snapshot.as_of.isoformat()
    assert brief["published_at"] == pair.published_at.isoformat()
    assert brief["reference"] == context.assessment.reference.model_dump(mode="json")
    assert brief["stance"] == pair.document.conclusion.stance
    assert "exact reference" in brief["display"]

    discovered = load_generated_selection_context(
        request=selected.request,
        generation_id=selected.inputs.generation.generation_id,
        screening_selection_id=selected.inputs.screening_selection_id,
        research_as_of=NOW.date(),
        known_at=NOW,
        runs_path=runs,
        db_path=app,
        comparison_tickers=("1234",),
    )
    assert discovered["saved_company_context"]["1234"]["assessment"] == brief
    assert discovered["candidate_tickers"] == []  # No promotion or ranking from the timing text.
    assert (
        research_catalog(app, as_of=NOW.date(), known_at=NOW - timedelta(seconds=1))["items"] == []
    )
    assert context.model_dump_json() == original
    assert selection_sha256(selected) == selection_digest
    with closing(connect_read_only(app)) as connection:
        assert load_reviewed_assessment(connection, pair.assessment_id) == pair
    assert (app.read_bytes(), runs.read_bytes()) == before


def test_each_realization_field_has_its_own_bound_and_exact_context_is_unchanged(tmp_path):
    realization = {
        "mechanism": "機序" * 400,
        "timing": "uncertain",
        "timing_reason": "作者時期理由" * 400,
        "delay_risk": "回復遅延と待機のリスク" * 400,
    }
    _, context = _save(tmp_path / "app", realization)
    original = context.model_dump_json()
    brief = company_context_brief(context)["assessment"]
    assert brief["realization"] == {
        "mechanism": realization["mechanism"][:400],
        "timing": "uncertain",
        "timing_reason": realization["timing_reason"][:240],
        "delay_risk": realization["delay_risk"][:240],
    }
    brief["realization"]["delay_risk"] = "changed only in the returned brief"
    assert context.model_dump_json() == original
