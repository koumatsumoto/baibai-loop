"""公開済み入力を差し替えず、実Operation入口で固定する。"""

from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from tests.engine.investment_judgment.workflow.test_growth_projection_copy import growth_payload
from tests.helpers.assessment_history import publish_at_declared_time
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection

from baibai_engine.investment_judgment.market_assessment.models import (
    MarketContextAssessment,
    StoredAssessment,
)
from baibai_engine.investment_judgment.market_assessment.projection import assessment_projection
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.workspace.selected_prepare import (
    _generated_selection,
    prepare_selected_workspace,
    resume_selected_workspace,
)
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.storage.sqlite.application.read import connect_read_only

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")


@pytest.mark.parametrize("entry", ["start", "prepare"])
@pytest.mark.parametrize(
    ("fault_ticker", "fault", "error"),
    [
        ("1234", "old_date", r"periods\.0\.period_start"),
        ("2345", "old_date", r"periods\.0\.period_start"),
        ("1234", "unused_shares", "average_shares.value"),
        ("2345", "unused_shares", "average_shares.value"),
        ("2345", "wrong_ticker", "different subject"),
        ("3456", "unused_shares", None),  # 同じ世代だがこのCaseに固定しない会社。
        (None, None, None),
    ],
)
def test_new_case_checks_only_the_full_growth_reports_it_seals(
    tmp_path, entry, fault_ticker, fault, error
):
    runs, app, ws = tmp_path / "runs", tmp_path / "app", tmp_path / "ws"
    tickers = ("1234", "2345", "3456")
    history = growth_payload("current")
    models = []
    for ticker in tickers:
        raw = {**deepcopy(history), "ticker": ticker}
        if ticker == fault_ticker:
            if fault == "old_date":
                raw["periods"][0]["period_start"] = "invalid date"
            elif fault == "unused_shares":
                reported = next(
                    obs
                    for obs in raw["periods"][0]["observations"]
                    if obs["measurement"] == "reported"
                )
                reported["metrics"]["average_shares"]["value"] = "invalid shares"
            else:
                raw["ticker"] = "9999"
        models.append(
            GeneratedModel(
                model_id="company_growth",
                calculation_revision="fixture",
                subject=ticker,
                payload=raw,
            )
        )
    source = seed_generated_selection(runs=runs, now=NOW, tickers=tickers, models=models)
    draft = generated_draft_for(source).model_copy(
        update={"targets": source.targets[:1], "comparison_tickers": ("2345",)}
    )
    service = OperationService(app, runs_path=runs)
    if entry == "start":
        # Same authentic source slice, even when the supplied model itself is malformed.
        fixed = _generated_selection(draft, runs_path=runs, app_path=app)
        supplied = OperationPayload(checkpoint="start", artifacts=(fixed.model_dump(mode="json"),))

        def start():
            return service.start(
                session_kind="capital-allocation",
                as_of=NOW.date(),
                started_at=NOW,
                payload=supplied,
            )
    else:

        def start():
            return prepare_selected_workspace(
                draft=draft, db_path=app, runs_path=runs, workspace=ws, now=NOW
            )

    original = runs.read_bytes()
    if error:
        with pytest.raises(ValueError, match=error):
            start()
        assert service.active() is None
        assert not app.exists()  # 未完結果・defer提案を記録できるOperationさえ作らない。
        assert not (ws / "manifest.yaml").exists()
    else:
        op = service.get(start().operation_id)
        fixed = selection_binding(op.payload)
        assert op.status == "active"
        assert fixed.evidence_subjects() == ("1234", "2345")
        assert {m.subject for m in fixed.inputs.generation.models} == {"1234", "2345"}
    assert runs.read_bytes() == original


@pytest.mark.parametrize(
    "change",
    [
        "absent",
        "omitted",
        "summary",
        "future",
        "false_age",
        "float_schema",
        "null_age",
        "canonical",
        "reordered",
    ],
)
def test_start_requires_exact_market_judgment_at_selection_cutoff(tmp_path, change):
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
        publish_at_declared_time(app, document, expected_head=None)
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=app)
    canonical = assessment_projection(
        replace(
            StoredAssessment.from_payload(document.payload()), recorded_at=document.published_at
        ),
        as_of=NOW.date(),
    )
    supplied = None if change == "omitted" else dict(canonical)
    if change == "summary":
        supplied["summary"] = "公開本文とは違う判断。"
    if change == "future":
        supplied["published_at"] = (NOW + timedelta(days=1)).isoformat()
    if change == "false_age":
        supplied["age_days"] = False
    if change == "float_schema":
        supplied["schema_version"] = 6.0
    if change == "null_age":
        supplied["age_days"] = None
    if change == "reordered":
        supplied = dict(reversed(tuple(canonical.items())))
    raw = selected.model_dump(mode="json")
    raw["inputs"]["saved_market_context_assessment"] = supplied

    def admit():
        return OperationService(app, runs_path=runs).start(
            session_kind="capital-allocation",
            as_of=NOW.date(),
            started_at=NOW,
            payload=OperationPayload(checkpoint="start", artifacts=(raw,)),
        )

    before = None
    if app.exists():
        with closing(connect_read_only(app)) as connection:
            before = connection.execute(
                "SELECT context_id,payload FROM market_context_assessment"
            ).fetchall()
    if change not in {"canonical", "reordered"}:
        with pytest.raises(
            ValueError, match="saved Market Assessment differs from the selected cutoff"
        ):
            admit()
        assert OperationService(app).active() is None
    else:
        operation = admit()
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
    prior = MarketContextAssessment(
        context_id="market-context-assessment-2026-10-14-prior",
        as_of=NOW.date(),
        known_at=NOW - timedelta(minutes=2),
        published_at=NOW - timedelta(minutes=1),
        summary="選定時に公開されていた判断。",
        inputs=(),
    )
    if has_prior:
        publish_at_declared_time(app, prior, expected_head=None)
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=app)
    later = prior.model_copy(
        update={
            "context_id": "market-context-assessment-2026-10-14-later",
            "published_at": NOW + timedelta(minutes=1),
            "summary": "選定後に公開された判断。",
        }
    )
    publish_at_declared_time(app, later, expected_head=prior.context_id if has_prior else None)
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
        assessment_projection(
            replace(StoredAssessment.from_payload(prior.payload()), recorded_at=prior.published_at),
            as_of=NOW.date(),
        )
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
