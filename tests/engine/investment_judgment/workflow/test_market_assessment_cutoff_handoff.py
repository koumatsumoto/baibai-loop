"""市場判断の情報締切を、発行/選定時計へ置き換えず執筆入力へ渡す。"""

from datetime import datetime, timedelta

import pytest
import yaml
from pydantic import ValidationError
from tests.helpers.assessment_history import historical_context_payload
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection

from baibai_engine.investment_judgment.market_assessment.models import (
    MarketContextAssessment,
    StoredAssessment,
)
from baibai_engine.investment_judgment.market_assessment.projection import assessment_projection
from baibai_engine.investment_judgment.market_assessment.query import assessment_by_id
from baibai_engine.investment_judgment.market_assessment.service import (
    AssessmentConflictError,
    AssessmentService,
)
from baibai_engine.investment_judgment.workflow.inputs import project_research_inputs
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.workspace.selected_prepare import (
    prepare_selected_workspace,
    resume_selected_workspace,
)

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


def _document():
    return MarketContextAssessment.model_validate(
        {
            "context_id": "market-context-assessment-2026-10-14-cutoff",
            "as_of": "2026-10-14",
            "known_at": (NOW - timedelta(hours=3)).isoformat(),
            "published_at": (NOW - timedelta(hours=1)).isoformat(),
            "summary": "合成：金利変化が借換費用へ届く経路を個社で調べる。",
            "inputs": [
                {
                    "input_id": "rates",
                    "reference": "https://example.invalid/synthetic-rates",
                    "layer": "primary_source",
                    "observation_as_of": "2026-10-13",
                    "available_at": (NOW - timedelta(hours=4)).isoformat(),
                }
            ],
            "uncertainties": [{"summary": "締切後の新情報を確認済みとはしない。"}],
            "implications": [
                {
                    "summary": "借入の変動/固定と借換時点を確認し、価格下落だけで価値を増やさない。",
                    "source_ids": ["rates"],
                }
            ],
        }
    )


def _prepare(tmp_path):
    app, runs = tmp_path / "app.sqlite", tmp_path / "runs.sqlite"
    prior = _document()
    AssessmentService(app).publish(prior, expected_head=None)
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=app)
    prepared = prepare_selected_workspace(
        draft=generated_draft_for(selected),
        db_path=app,
        runs_path=runs,
        workspace=tmp_path / "workspace",
        now=NOW,
    )
    return app, prior, prepared


def test_market_cutoff_survives_normal_selection_and_resume(tmp_path, monkeypatch):
    app, prior, prepared = _prepare(tmp_path)
    selected = selection_binding(OperationService(app).get(prepared.operation_id).payload)
    projected = project_research_inputs(selected)["saved_market_context_assessment"]
    assert projected["known_at"] == prior.known_at.isoformat()
    assert projected["known_at"] != projected["published_at"]
    assert projected["known_at"] != selected.input_known_at.isoformat()
    assert projected["sources"] == prior.payload()["inputs"]
    assert projected["implications"] == prior.payload()["implications"]
    assert projected["uncertainties"] == prior.payload()["uncertainties"]
    input_path = prepared.workspace / "domain-inputs.yaml"
    assert yaml.safe_load(input_path.read_text())["saved_market_context_assessment"] == projected
    before_retry = app.read_bytes()
    assert AssessmentService(app).publish(prior, expected_head=None) == prior
    assert app.read_bytes() == before_retry

    later = prior.model_copy(
        update={
            "context_id": "market-context-assessment-2026-10-14-later",
            "known_at": NOW + timedelta(minutes=1),
            "published_at": NOW + timedelta(minutes=2),
            "summary": "選定後の別判断。旧入力を置き換えない。",
        }
    )
    AssessmentService(app).publish(later, expected_head=prior.context_id)
    before = app.read_bytes(), input_path.read_bytes()
    with pytest.raises(AssessmentConflictError, match="different publication state"):
        AssessmentService(app).publish(prior, expected_head=None)
    assert assessment_by_id(app, context_id=prior.context_id).document == prior.payload()
    assert app.read_bytes() == before[0]
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.workflow.workspace.selected_prepare.latest_assessment",
        lambda *_a, **_kw: pytest.fail("resume selected a later Market Assessment"),
    )
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=app,
        workspace=prepared.workspace,
        now=NOW + timedelta(minutes=3),
        force=True,
    )
    assert resumed.selection_sha256 == prepared.selection_sha256
    assert (app.read_bytes(), input_path.read_bytes()) == before


@pytest.mark.parametrize("version", [4, 5, 6])
def test_historical_cutoff_is_not_inferred_from_publication_or_sources(version):
    payload = historical_context_payload() if version == 4 else _document().payload()
    payload["schema_version"] = version
    if version == 5:
        payload.pop("known_at")
    before = str(payload)
    projected = assessment_projection(StoredAssessment.from_payload(payload), as_of=NOW.date())
    assert projected["known_at"] == payload.get("known_at")
    assert projected["published_at"] == payload["published_at"]
    assert str(payload) == before


def test_source_after_market_cutoff_is_still_rejected():
    payload = _document().payload()
    payload["inputs"][0]["available_at"] = (
        _document().known_at + timedelta(microseconds=1)
    ).isoformat()
    with pytest.raises(ValidationError, match="unavailable at known_at"):
        MarketContextAssessment.model_validate(payload)


def test_missing_judgment_does_not_invent_a_cutoff():
    assert assessment_projection(None, as_of=NOW.date())["known_at"] is None


def test_later_publication_is_not_included_at_the_selection_cutoff(tmp_path):
    app, runs = tmp_path / "app.sqlite", tmp_path / "runs.sqlite"
    later = _document().model_copy(update={"published_at": NOW + timedelta(minutes=1)})
    AssessmentService(app).publish(later, expected_head=None)
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=app)
    prepared = prepare_selected_workspace(
        draft=generated_draft_for(selected),
        db_path=app,
        runs_path=runs,
        workspace=tmp_path / "workspace",
        now=NOW + timedelta(minutes=2),
    )
    saved = selection_binding(OperationService(app).get(prepared.operation_id).payload)
    assert project_research_inputs(saved)["saved_market_context_assessment"] is None
    assert assessment_by_id(app, context_id=later.context_id).document == later.payload()


def test_legacy_fixed_selection_resume_does_not_rewrite_the_saved_projection(tmp_path, monkeypatch):
    def old_projection(record, *, as_of):
        projected = assessment_projection(record, as_of=as_of)
        projected.pop("known_at", None)
        return projected

    with monkeypatch.context() as legacy:
        for module in (
            "workflow.workspace.selected_prepare",
            "workflow.operation.validation",
        ):
            legacy.setattr(
                f"baibai_engine.investment_judgment.{module}.assessment_projection", old_projection
            )
        app, prior, prepared = _prepare(tmp_path)
    input_path = prepared.workspace / "domain-inputs.yaml"
    before = app.read_bytes(), input_path.read_bytes()
    assert "known_at" not in yaml.safe_load(before[1])["saved_market_context_assessment"]
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=app,
        workspace=prepared.workspace,
        now=NOW + timedelta(minutes=1),
        force=True,
    )
    assert resumed.selection_sha256 == prepared.selection_sha256
    assert (app.read_bytes(), input_path.read_bytes()) == before
    assert assessment_by_id(app, context_id=prior.context_id).document == prior.payload()
