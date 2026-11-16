"""過去の採否と最強反証を分けて固定し、再訪で支持だけを引き継がない。"""

from datetime import datetime, timedelta
from hashlib import sha256

import pytest
import yaml
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection

from baibai_engine.investment_judgment.company_research.context import (
    CompanyContext,
    NotFound,
    Unreadable,
    company_context_brief,
)
from baibai_engine.investment_judgment.company_research.query import (
    read_company_contexts,
    research_catalog,
)
from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import parse_selection, selection_sha256
from baibai_engine.investment_judgment.workflow.selection_query import (
    load_generated_selection_context,
)
from baibai_engine.investment_judgment.workflow.workspace import (
    prepare_selected_workspace,
    resume_selected_workspace,
)
from baibai_engine.primitives.json import encode_json

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")
COUNTERCASE = "営業回復の観測だけでは投資後 cash と普通株価値を裏付けない。借換と希薄化が反対経路。"


def _save(
    path,
    countercase=COUNTERCASE,
    *,
    stance="supported",
    number=1,
    now=NOW,
    supersedes=None,
    conditions=None,
):
    document, review = pair_payload(review_number=number + 10)
    document["conclusion"].update(stance=stance, strongest_countercase=countercase)
    document["investment_case"]["status"] = {
        "supported": "intact",
        "uncertain": "uncertain",
        "unsupported": "broken",
    }[stance]
    # The required conclusion is not required to be copied into these separate sections.
    document["findings"]["hypotheses"] = []
    document["findings"]["unresolved_questions"] = []
    if conditions is not None:
        document["investment_case"]["invalidation_conditions"] = conditions
    review["strongest_countercase"] = f"独立Review {number} の確認範囲に限る反証"
    update_review_hash(document, review)
    return AssessmentStoreService(path, clock=lambda: now).publish_reviewed_assessment(
        assessment_id(number), document, review, supersedes_id=supersedes
    )


def _context(path):
    return read_company_contexts(path, tickers=("1234",), as_of=NOW.date(), known_at=NOW)["1234"]


@pytest.mark.parametrize("stance", ["supported", "uncertain", "unsupported"])
def test_countercase_follows_exact_assessment_to_catalog_and_candidate_context(tmp_path, stance):
    app, runs = tmp_path / "app", tmp_path / "runs"
    pair = _save(app, stance=stance)
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=(), db=app)
    before = app.read_bytes(), runs.read_bytes()
    context = _context(app)
    assert (
        context.assessment.invalidation_conditions
        == pair.document.investment_case.invalidation_conditions
    )
    assert context.assessment.strongest_countercase == COUNTERCASE
    brief = research_catalog(app, as_of=NOW.date(), known_at=NOW)["items"][0]["assessment"]
    assert brief["strongest_countercase"] == COUNTERCASE
    assert brief["strongest_countercase_truncated"] is False
    assert brief["stance"] == stance
    assert brief["reference"]["core_sha256"] == pair.core_sha256
    assert brief["role"] == "historical_reviewed_assessment_not_current_readiness"
    assert brief["as_of"] == pair.document.input_snapshot.as_of.isoformat()
    assert brief["published_at"] == pair.published_at.isoformat()
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
    assert discovered["candidate_tickers"] == []
    assert (
        research_catalog(app, as_of=NOW.date(), known_at=NOW - timedelta(seconds=1))["items"] == []
    )
    assert (app.read_bytes(), runs.read_bytes()) == before


def test_long_countercase_is_bounded_only_in_brief(tmp_path):
    text = "利益回復を価値へ直結させない。" * 80
    _save(tmp_path / "app", text)
    context = _context(tmp_path / "app")
    original = context.model_dump_json()
    brief = company_context_brief(context)["assessment"]
    assert brief["strongest_countercase"] == text[:400]
    assert brief["strongest_countercase_truncated"] is True
    brief["strongest_countercase"] = "returned copy only"
    assert context.assessment.strongest_countercase == text
    assert context.model_dump_json() == original


def test_prepare_and_resume_keep_countercase_without_later_assessment_substitution(
    tmp_path, monkeypatch
):
    app, runs, workspace = tmp_path / "app", tmp_path / "runs", tmp_path / "workspace"
    pair = _save(app)
    seed = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=app)
    prepared = prepare_selected_workspace(
        draft=generated_draft_for(seed), db_path=app, workspace=workspace, runs_path=runs, now=NOW
    )
    operation = OperationService(app).get(prepared.operation_id)
    selected = selection_binding(operation.payload)
    saved = selected.inputs.saved_company_context["1234"].assessment
    assert saved.invalidation_conditions == pair.document.investment_case.invalidation_conditions
    assert saved.valuation_context.status == pair.document.valuation.status
    assert saved.independent_review.strongest_countercase == pair.review.strongest_countercase
    assert saved.strongest_countercase == COUNTERCASE
    assert saved.reference.core_sha256 == pair.core_sha256
    inputs = workspace / "domain-inputs.yaml"
    before = inputs.read_bytes()
    projected = yaml.safe_load(before)
    assert (
        projected["saved_company_context"]["1234"]["assessment"]["strongest_countercase"]
        == COUNTERCASE
    )
    digest = selection_sha256(selected)
    _save(
        app,
        "後発の別反証を旧固定入力へ差し替えない。",
        number=2,
        now=NOW + timedelta(seconds=1),
        supersedes=pair.assessment_id,
    )
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.company_research.query.company_context",
        lambda *a, **kw: pytest.fail("resume must not reread latest company context"),
    )
    resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=app,
        workspace=workspace,
        now=NOW + timedelta(seconds=2),
    )
    assert inputs.read_bytes() == before
    assert (
        selection_sha256(
            selection_binding(OperationService(app).get(prepared.operation_id).payload)
        )
        == digest
    )


def test_legacy_selection_does_not_backfill_or_change_canonical_identity(tmp_path):
    app, runs = tmp_path / "app", tmp_path / "runs"
    _save(app)
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=app)
    raw = selected.model_dump(mode="json")
    raw["inputs"]["saved_company_context"]["1234"]["assessment"].pop("strongest_countercase", None)
    raw["inputs"]["saved_company_context"]["1234"]["assessment"].pop(
        "invalidation_conditions", None
    )
    raw["inputs"]["saved_company_context"]["1234"]["assessment"].pop("independent_review", None)
    raw["inputs"]["saved_company_context"]["1234"]["assessment"].pop("valuation_context", None)
    canonical = encode_json(raw)
    parsed = parse_selection(raw)
    assert encode_json(parsed.model_dump(mode="json")) == canonical
    assert selection_sha256(parsed) == sha256(canonical.encode()).hexdigest()
    brief = company_context_brief(parsed.inputs.saved_company_context["1234"])["assessment"]
    assert "strongest_countercase" not in brief
    assert "strongest_countercase_truncated" not in brief
    assert "valuation_context" not in brief
    assert "independent_review" not in brief
    assert "invalidation_conditions" not in brief
    assert brief["stance"] == "supported"


@pytest.mark.parametrize(
    "assessment", [NotFound(), Unreadable(record_id="broken", reason="unreadable")]
)
def test_absent_or_unreadable_context_does_not_invent_a_countercase(assessment):
    context = CompanyContext(research=NotFound(), assessment=assessment)
    assert "strongest_countercase" not in company_context_brief(context)["assessment"]


def test_invalidation_conditions_remain_authored_historical_conditions(tmp_path):
    conditions = [
        "主力増分が継続全社費用を吸収できない。",
        "投資後cashが残らない。",
        "長い条件" * 140,
        "追加条件",
    ]
    pair = _save(tmp_path / "app", conditions=conditions)
    context = _context(tmp_path / "app")
    before = context.model_dump_json()
    brief = company_context_brief(context)["assessment"]
    assert context.assessment.invalidation_conditions == tuple(conditions)
    assert brief["invalidation_conditions"] == [
        {"text": text[:400], "text_truncated": len(text) > 400} for text in conditions[:3]
    ]
    assert brief["invalidation_conditions_omitted"] == 1
    assert brief["reference"]["core_sha256"] == pair.core_sha256
    assert brief["stance"] == "supported"
    brief["invalidation_conditions"][0]["text"] = "not a new judgment"
    assert context.model_dump_json() == before
