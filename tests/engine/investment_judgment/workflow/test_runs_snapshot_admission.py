"""CaseとGenerationは、準備・受付それぞれで同じRunsから読む。"""

import gzip
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from shutil import copyfile
from uuid import UUID

import pytest
from tests.engine.investment_judgment.workflow.test_discovery_questions import observation
from tests.helpers.model_generation import publish_generation
from tests.helpers.research_selection import selected_target
from tests.helpers.retained_model_generation import seed_generation

from baibai_engine.investment_judgment.workflow import case_screening, selection_query
from baibai_engine.investment_judgment.workflow.operation import service
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    ResearchRequest,
    selection_payload_and_sha256,
)
from baibai_engine.investment_judgment.workflow.workspace import selected_prepare
from baibai_engine.machine_analysis.generation import query as generation_query
from baibai_engine.machine_analysis.generation.contracts import GenerationSubject, ModelGeneration
from baibai_engine.machine_analysis.screening.contracts import ScreeningSelection
from baibai_engine.machine_analysis.screening.discovery_contract import (
    DiscoveryPolicy,
    DiscoverySelection,
    discovery_publication_id,
)
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.primitives.json import encode_json

NOW = datetime(2026, 10, 25, 9, tzinfo=UTC)
AS_OF = date(2026, 10, 23)
STOCKS = ("1234", "2345", "3456")


def seed_runs(path, *, edition):
    old = seed_generation(path, now=NOW, as_of=AS_OF)
    generation_id = old.generation_id
    selection_id = "policy-1" if edition == "explicit_legacy" else None
    if edition == "current":
        generation_id = "snapshot-current"
        generation = ModelGeneration(
            schema_version=2,
            generation_id=generation_id,
            as_of=AS_OF,
            evaluation_time=NOW - timedelta(minutes=1),
            inputs=old.inputs,
            subject_plan={
                **{
                    t: GenerationSubject(
                        scope="stock",
                        model_ids=tuple(m.model_id for m in old.models if m.subject == "1234"),
                    )
                    for t in STOCKS
                },
                "market": GenerationSubject(scope="market", model_ids=("market_context",)),
            },
            models=old.models,
        )
        publish_generation(generation, path=path)
        selection_id = discovery_publication_id(generation_id)
        ScreeningSelectionStore(path).publish(
            selection_id,
            ScreeningSelection(
                generation_id,
                STOCKS,
                DiscoverySelection(
                    DiscoveryPolicy(),
                    tuple(
                        observation(
                            ticker,
                            price=900,
                            price_date=AS_OF.isoformat(),
                            expected_price_date=AS_OF.isoformat(),
                            basis="last_close_unadjusted",
                            price_ready=True,
                        )
                        for ticker in STOCKS
                    ),
                    {},
                ),
            ),
        )
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "UPDATE screening_selection SET published_at=? WHERE selection_id=?",
                ((NOW - timedelta(seconds=30)).isoformat(), selection_id),
            )
    return GeneratedResearchSelectionDraft(
        schema_version=2,
        request=ResearchRequest(
            text="合成の固定断面で比較する", scope="specified_stocks", requested_tickers=("1234",)
        ),
        research_as_of=AS_OF,
        input_known_at=NOW,
        generation_id=generation_id,
        screening_selection_id=selection_id,
        selection_summary="当時の価格とモデルを照合する",
        targets=(selected_target("1234"),),
        comparison_tickers=("2345",) if selection_id is not None else (),
    )


def changed_copy(source, dest, draft):
    copyfile(source, dest)
    # 通常の発行は同IDかつ異なる本文を拒否する。ここでは異なる復元元を合成する。
    with closing(sqlite3.connect(dest)) as db, db:
        (blob,) = db.execute(
            "SELECT payload_gzip FROM screening_selection WHERE selection_id=?",
            (draft.screening_selection_id,),
        ).fetchone()
        selection = json.loads(gzip.decompress(blob))
        selection["result"]["rows"][0]["price"] = 1001
        db.execute(
            "UPDATE screening_selection SET payload_gzip=? WHERE selection_id=?",
            (gzip.compress(encode_json(selection).encode(), mtime=0), draft.screening_selection_id),
        )
        key = (draft.generation_id, "technical_analysis", "1234")
        where = " WHERE generation_id=? AND model_id=? AND subject=?"
        (blob,) = db.execute("SELECT payload_gzip FROM generated_model" + where, key).fetchone()
        model = json.loads(gzip.decompress(blob))
        model["synthetic_replacement"] = True
        db.execute(
            "UPDATE generated_model SET payload_gzip=?" + where,
            (gzip.compress(encode_json(model).encode(), mtime=0), *key),
        )


def after_read(monkeypatch, module, identity, callback):
    original = getattr(module, identity)

    def read(connection, *args, **kwargs):
        result = original(connection, *args, **kwargs)
        assert connection.in_transaction
        callback(connection)
        return result

    monkeypatch.setattr(module, identity, read)


def test_serialized_start_rejects_mixed_runs_evidence_after_same_id_file_swap(
    tmp_path, monkeypatch
):
    runs, replacement, app = (tmp_path / name for name in ("runs", "replacement", "app"))
    draft = seed_runs(runs, edition="current")
    changed_copy(runs, replacement, draft)
    first, second = (
        selected_prepare._generated_selection(draft, runs_path=path, app_path=app)
        for path in (runs, replacement)
    )
    assert first.inputs.screening_selection != second.inputs.screening_selection
    assert first.inputs.generation != second.inputs.generation
    mixed = second.model_copy(
        update={"inputs": second.inputs.model_copy(update={"generation": first.inputs.generation})}
    )
    payload = OperationPayload(checkpoint="start", artifacts=(mixed.model_dump(mode="json"),))
    after_read(monkeypatch, generation_query, "_read_metadata", lambda _: replacement.replace(runs))

    with pytest.raises(service.OperationConflictError, match="screening selection differs"):
        service.OperationService(app, runs_path=runs).start(
            session_kind="capital-allocation", as_of=AS_OF, started_at=NOW, payload=payload
        )
    assert not replacement.exists()  # SELECT後の入れ替えは実際に起きた。
    assert not app.exists()


def test_prepare_rejects_cross_phase_replacement_even_when_runs_path_swaps_back(
    tmp_path, monkeypatch
):
    runs, later, original, app, ws = (tmp_path / f for f in ("runs", "B", "A", "app", "workspace"))
    draft = seed_runs(runs, edition="current")
    copyfile(runs, original)
    changed_copy(runs, later, draft)
    # Caseを読んだ準備中にA→B。独立受付のモデル読取中にはB→A。
    replacements = [later, original]

    def swap_after_prepare_case(_connection):
        if len(replacements) == 2:
            replacements.pop(0).replace(runs)

    def swap_during_admission_models(_connection):
        # 準備側のモデル読取でなく、2回目だけを置き換える。
        nonlocal model_reads
        model_reads += 1
        if model_reads == 2:
            replacements.pop(0).replace(runs)

    model_reads = 0
    after_read(
        monkeypatch,
        case_screening,
        "read_case_publication_from_connection",
        swap_after_prepare_case,
    )
    after_read(monkeypatch, generation_query, "_read_metadata", swap_during_admission_models)

    with pytest.raises(ValueError, match="model input slice differs"):
        selected_prepare.prepare_selected_workspace(
            draft=draft, db_path=app, workspace=ws, runs_path=runs, now=NOW
        )
    assert model_reads == 2
    assert replacements == []
    assert not app.exists()
    assert not ws.exists()


@pytest.mark.parametrize("edition", ["current", "explicit_legacy", "generation_only"])
def test_identical_replacement_preserves_frozen_payload_and_projection(
    tmp_path, monkeypatch, edition
):
    runs = tmp_path / "runs"
    draft = seed_runs(runs, edition=edition)
    monkeypatch.setattr(service, "uuid4", lambda: UUID(int=1))

    def prepare(name):
        ws, app = tmp_path / name, tmp_path / f"app-{name}"
        prepared = selected_prepare.prepare_selected_workspace(
            draft=draft, db_path=app, workspace=ws, runs_path=runs, now=NOW
        )
        saved = service.OperationService(app).get(prepared.operation_id)
        fixed = selection_binding(saved.payload)
        return (
            saved.model_dump_json(),
            selection_payload_and_sha256(fixed),
            (ws / "manifest.yaml").read_bytes(),
            (ws / "domain-inputs.yaml").read_bytes(),
        )

    first = prepare("first")
    first_copy, second_copy = tmp_path / "same-for-prepare", tmp_path / "same-for-admission"
    for path in (first_copy, second_copy):
        copyfile(runs, path)
    replacements = [first_copy, second_copy]
    after_read(
        monkeypatch, generation_query, "_read_metadata", lambda _: replacements.pop(0).replace(runs)
    )
    assert prepare("replacement") == first
    assert replacements == []


@pytest.mark.parametrize("reader", ["context", "prepare"])
@pytest.mark.parametrize("case", ["wrong_owner", "same_owner", "future_wrong_owner", "absent"])
def test_explicit_selection_checks_visible_owner_before_reading_body(
    tmp_path, monkeypatch, reader, case
):
    runs = tmp_path / "runs"
    draft = seed_runs(runs, edition="current")
    with closing(sqlite3.connect(runs)) as connection, connection:
        connection.execute(
            "UPDATE screening_selection SET payload_gzip=?,published_at=? WHERE selection_id=?",
            (
                b"broken body",
                (NOW + timedelta(seconds=1) if case == "future_wrong_owner" else NOW).isoformat(),
                draft.screening_selection_id,
            ),
        )
    if case == "absent":
        draft = draft.model_copy(update={"screening_selection_id": "not-found"})
    elif case != "same_owner":
        # A real other generation exists in this same Runs DB.
        draft = draft.model_copy(update={"generation_id": "fixed-synthetic"})

    statements = []
    owner = selection_query if reader == "context" else selected_prepare
    original_connect = owner.connect_runs_read_only

    def traced(path):
        connection = original_connect(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(owner, "connect_runs_read_only", traced)
    error = gzip.BadGzipFile if case == "same_owner" else ValueError
    expected = (
        "Not a gzipped file"
        if case == "same_owner"
        else "unknown or not yet published screening selection"
        if reader == "context" and case in {"future_wrong_owner", "absent"}
        else "screening selection differs from the chosen generation"
    )

    def read():
        if reader == "context":
            selection_query.load_generated_selection_context(
                request=draft.request,
                generation_id=draft.generation_id,
                screening_selection_id=draft.screening_selection_id,
                research_as_of=draft.research_as_of,
                known_at=draft.input_known_at,
                runs_path=runs,
                db_path=tmp_path / "app",
            )
        else:
            selected_prepare.prepare_selected_workspace(
                draft=draft,
                runs_path=runs,
                db_path=tmp_path / "app",
                workspace=tmp_path / "workspace",
                now=NOW,
            )

    with pytest.raises(error, match=expected):
        read()
    assert statements
    assert any("payload_gzip" in sql for sql in statements) == (case == "same_owner")
    assert not (tmp_path / "app").exists()
    assert not (tmp_path / "workspace").exists()
