"""公開済み入力を差し替えず、実Operation入口で固定する。"""

import json
from contextlib import closing
from datetime import datetime, timedelta

import pytest
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection

from baibai_engine.cli.operation import main
from baibai_engine.investment_judgment.market_assessment.models import (
    MarketContextAssessment,
    StoredAssessment,
)
from baibai_engine.investment_judgment.market_assessment.projection import assessment_projection
from baibai_engine.investment_judgment.market_assessment.service import AssessmentService
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.workspace.selected_prepare import (
    prepare_selected_workspace,
    resume_selected_workspace,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


@pytest.mark.parametrize("change", ["absent", "omitted", "summary", "future", "canonical"])
def test_start_requires_exact_market_judgment_at_selection_cutoff(tmp_path, capsys, change):
    runs, app = tmp_path / "runs", tmp_path / "app"
    document = MarketContextAssessment(
        context_id="market-context-assessment-2026-10-14-fixed",
        as_of=NOW.date(),
        known_at=NOW - timedelta(minutes=2),
        published_at=NOW - timedelta(minutes=1),
        summary="固定時点の市場判断。",
        inputs=(),
    )
    if change != "absent":
        AssessmentService(app).publish(document, expected_head=None)
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=app)
    canonical = assessment_projection(
        StoredAssessment.from_payload(document.payload()), as_of=NOW.date()
    )
    supplied = None if change == "omitted" else dict(canonical)
    if change == "summary":
        supplied["summary"] = "公開本文とは違う判断。"
    if change == "future":
        supplied["published_at"] = (NOW + timedelta(days=1)).isoformat()
    raw = selected.model_dump(mode="json")
    raw["inputs"]["saved_market_context_assessment"] = supplied
    payload = tmp_path / "payload.json"
    payload.write_text(OperationPayload(checkpoint="start", artifacts=(raw,)).model_dump_json())
    before = None
    if app.exists():
        with closing(connect_read_only(app)) as connection:
            before = connection.execute(
                "SELECT context_id,payload FROM market_context_assessment"
            ).fetchall()
    result = main(
        [
            "--db",
            str(app),
            "--runs-path",
            str(runs),
            "--format",
            "json",
            "start",
            "--kind",
            "capital-allocation",
            "--as-of",
            NOW.date().isoformat(),
            "--payload",
            str(payload),
        ],
        now=NOW,
    )
    if change != "canonical":
        assert result == 1
        assert "saved Market Assessment differs from the selected cutoff" in capsys.readouterr().err
        assert OperationService(app).active() is None
    else:
        assert result == 0
        published = json.loads(capsys.readouterr().out)
        operation = OperationService(app).get(published["operation_id"])
        assert (
            selection_binding(operation.payload).inputs.saved_market_context_assessment == canonical
        )
        before_resume = app.read_bytes()
        resume_selected_workspace(
            operation_id=operation.operation_id,
            db_path=app,
            workspace=tmp_path / "workspace",
            now=NOW,
        )
        assert app.read_bytes() == before_resume
    if before is not None:
        with closing(connect_read_only(app)) as connection:
            assert (
                connection.execute(
                    "SELECT context_id,payload FROM market_context_assessment"
                ).fetchall()
                == before
            )


@pytest.mark.parametrize("has_prior", [False, True])
def test_prepare_and_resume_keep_cutoff_context_after_later_publication(tmp_path, has_prior):
    app, runs, workspace = tmp_path / "app", tmp_path / "runs", tmp_path / "workspace"
    writer = AssessmentService(app)
    prior = MarketContextAssessment(
        context_id="market-context-assessment-2026-10-14-prior",
        as_of=NOW.date(),
        known_at=NOW - timedelta(minutes=2),
        published_at=NOW - timedelta(minutes=1),
        summary="選定時に公開されていた判断。",
        inputs=(),
    )
    if has_prior:
        writer.publish(prior, expected_head=None)
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=app)
    later = prior.model_copy(
        update={
            "context_id": "market-context-assessment-2026-10-14-later",
            "published_at": NOW + timedelta(minutes=1),
            "summary": "選定後に公開された判断。",
        }
    )
    writer.publish(later, expected_head=prior.context_id if has_prior else None)
    prepared = prepare_selected_workspace(
        draft=generated_draft_for(selected),
        db_path=app,
        runs_path=runs,
        workspace=workspace,
        now=NOW + timedelta(minutes=2),
    )
    operation = OperationService(app).get(prepared.operation_id)
    saved = selection_binding(operation.payload)
    assert saved.inputs.saved_market_context_assessment == (
        assessment_projection(StoredAssessment.from_payload(prior.payload()), as_of=NOW.date())
        if has_prior
        else None
    )
    before = app.read_bytes(), (workspace / "domain-inputs.yaml").read_bytes()
    resumed = resume_selected_workspace(
        operation_id=prepared.operation_id,
        db_path=app,
        workspace=workspace,
        now=NOW + timedelta(minutes=3),
        force=True,
    )
    assert resumed.selection_sha256 == prepared.selection_sha256
    assert (app.read_bytes(), (workspace / "domain-inputs.yaml").read_bytes()) == before
