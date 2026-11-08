"""保存済み企業材料の型を、正本refを残したまま差し替えない。"""

import json
from contextlib import closing
from datetime import datetime, timedelta

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.generated_selection import seed_generated_selection

from baibai_engine.cli.operation import main
from baibai_engine.investment_judgment.company_research.document import ResearchDraft
from baibai_engine.investment_judgment.company_research.store import (
    ResearchStoreService,
    load_research,
)
from baibai_engine.investment_judgment.stock_assessment.store import (
    AssessmentStoreService,
    load_reviewed_assessment,
)
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import parse_selection, selection_sha256
from baibai_engine.investment_judgment.workflow.workspace import resume_selected_workspace
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.read import connect_read_only

NOW = datetime.fromisoformat("2026-10-14T18:00:00+09:00")
PUBLISHED_AT = NOW - timedelta(minutes=1)


def _inputs(tmp_path, value=0):
    app, runs = tmp_path / "app", tmp_path / "runs"
    document, review = pair_payload()
    fact = {
        "fact_id": "sample",
        "fact_kind": "other",
        "value": value,
        "unit": "count",
        "as_of": NOW.date().isoformat(),
        "source_ids": ["annual"],
    }
    document["input_snapshot"]["facts"].append(fact)
    update_review_hash(document, review)
    pair = AssessmentStoreService(app, clock=lambda: PUBLISHED_AT).publish_reviewed_assessment(
        assessment_id(), document, review
    )
    draft = ResearchDraft.model_validate(
        {
            "research_id": "research-company-context",
            "document": {
                "ticker": "1234",
                "as_of": NOW.date().isoformat(),
                "investigated_scope": "合成の観測事実の型を保持する",
                "sources": [document["input_snapshot"]["sources"][0]],
                "facts": [fact],
            },
        }
    )
    record = ResearchStoreService(app, clock=lambda: PUBLISHED_AT).save(draft)
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=app)
    return app, runs, selected, record, pair


def _start(tmp_path, app, runs, raw):
    payload = tmp_path / "payload.json"
    payload.write_text(OperationPayload(checkpoint="start", artifacts=(raw,)).model_dump_json())
    return main(
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


@pytest.mark.parametrize(("original", "replacement"), [(0, False), (1, 1.0)])
def test_start_rejects_assessment_fact_type_change(tmp_path, capsys, original, replacement):
    app, runs, selected, _, pair = _inputs(tmp_path, original)
    raw = selected.model_dump(mode="json")
    assessment = raw["inputs"]["saved_company_context"]["1234"]["assessment"]
    sample = next(fact for fact in assessment["facts"] if fact["fact_id"] == "sample")
    sample["value"] = replacement
    incoming = parse_selection(raw)
    changed = incoming.inputs.saved_company_context["1234"].assessment
    assert changed.reference == selected.inputs.saved_company_context["1234"].assessment.reference
    assert changed.reference.core_sha256 == pair.core_sha256
    assert type(changed.facts[-1].value) is type(replacement)
    assert selection_sha256(incoming) != selection_sha256(selected)
    before = app.read_bytes(), runs.read_bytes()

    assert _start(tmp_path, app, runs, raw) == 1
    assert "saved company context differs from the selected cutoff" in capsys.readouterr().err
    assert OperationService(app).active() is None
    assert (app.read_bytes(), runs.read_bytes()) == before
    with closing(connect_read_only(app)) as connection:
        saved = load_reviewed_assessment(connection, pair.assessment_id)
    assert canonical_json(saved.document.model_dump(mode="json")) == canonical_json(
        pair.document.model_dump(mode="json")
    )
    assert saved.core_sha256 == pair.core_sha256
    assert saved.published_at.isoformat() == PUBLISHED_AT.isoformat()


@pytest.mark.parametrize(("original", "replacement"), [(0, False), (1, 1.0)])
def test_start_retains_research_record_digest_guard(tmp_path, capsys, original, replacement):
    app, runs, selected, record, _ = _inputs(tmp_path, original)
    raw = selected.model_dump(mode="json")
    research = raw["inputs"]["saved_company_context"]["1234"]["research"]["record"]
    research["document"]["facts"][0]["value"] = replacement
    assert research["reference"] == record.reference.model_dump(mode="json")
    before = app.read_bytes(), runs.read_bytes()

    assert _start(tmp_path, app, runs, raw) == 1
    assert "Research payload digest mismatch" in capsys.readouterr().err
    assert OperationService(app).active() is None
    assert (app.read_bytes(), runs.read_bytes()) == before
    with closing(connect_read_only(app)) as connection:
        saved = load_research(connection, record.reference.research_id)
    assert canonical_json(saved.model_dump(mode="json")) == canonical_json(
        record.model_dump(mode="json")
    )


@pytest.mark.parametrize("shape", ["canonical", "reordered", "defaults_omitted"])
def test_start_accepts_typed_canonical_context_and_resume(tmp_path, capsys, monkeypatch, shape):
    app, runs, selected, record, pair = _inputs(tmp_path)
    raw = selected.model_dump(mode="json")
    context = raw["inputs"]["saved_company_context"]["1234"]
    assessment = context["assessment"]
    if shape == "reordered":
        assessment["facts"][-1] = dict(reversed(tuple(assessment["facts"][-1].items())))
        context["assessment"] = dict(reversed(tuple(assessment.items())))
        raw["inputs"]["saved_company_context"]["1234"] = dict(reversed(tuple(context.items())))
    elif shape == "defaults_omitted":
        assessment["facts"][-1].pop("observed_at")
        assessment["facts"][-1].pop("price_basis")
        research_document = context["research"]["record"]["document"]
        research_document.pop("unresolved_questions")
        research_document["facts"][0].pop("observed_at")
        research_document["facts"][0].pop("price_basis")
    expected_digest = selection_sha256(selected)
    assert selection_sha256(parse_selection(raw)) == expected_digest
    source_before = runs.read_bytes()

    assert _start(tmp_path, app, runs, raw) == 0
    identity = json.loads(capsys.readouterr().out)["operation_id"]
    saved = selection_binding(OperationService(app).get(identity).payload)
    assert selection_sha256(saved) == expected_digest
    assert canonical_json(saved.model_dump(mode="json")) == canonical_json(
        selected.model_dump(mode="json")
    )
    context = saved.inputs.saved_company_context["1234"]
    assert context.research.record.reference == record.reference
    assert context.research.record.recorded_at.isoformat() == PUBLISHED_AT.isoformat()
    assert context.assessment.reference.core_sha256 == pair.core_sha256
    assert context.assessment.published_at.isoformat() == PUBLISHED_AT.isoformat()
    assert type(context.assessment.facts[-1].value) is int

    def forbidden(*_args, **_kwargs):
        pytest.fail("resume must use its saved company context")

    monkeypatch.setattr(
        "baibai_engine.investment_judgment.workflow.operation.validation.company_context",
        forbidden,
    )
    monkeypatch.setattr(
        "baibai_engine.investment_judgment.workflow.workspace.selected_prepare."
        "read_company_contexts",
        forbidden,
    )
    workspace = tmp_path / "workspace"
    app_before = app.read_bytes()
    resumed = resume_selected_workspace(
        operation_id=identity, db_path=app, workspace=workspace, now=NOW
    )
    projection_before = (workspace / "domain-inputs.yaml").read_bytes()
    resumed_again = resume_selected_workspace(
        operation_id=identity, db_path=app, workspace=workspace, now=NOW, force=True
    )
    assert resumed.selection_sha256 == resumed_again.selection_sha256 == expected_digest
    assert (workspace / "domain-inputs.yaml").read_bytes() == projection_before
    assert app.read_bytes() == app_before
    assert runs.read_bytes() == source_before
