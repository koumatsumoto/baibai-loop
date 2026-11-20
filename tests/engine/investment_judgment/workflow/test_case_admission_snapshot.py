"""二つのstoreに分散Txを仮定せず、新しい有限Caseの実SQLite断面を検証する。"""

import gzip
import json
import sqlite3
import weakref
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.helpers.db_seed import seed_ledger
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.investment_judgment.company_research.document import ResearchDraft
from baibai_engine.investment_judgment.company_research.store import ResearchStoreService
from baibai_engine.investment_judgment.market_assessment.models import MarketContextAssessment
from baibai_engine.investment_judgment.market_assessment.service import AssessmentService
from baibai_engine.investment_judgment.workflow.operation import admission, service
from baibai_engine.investment_judgment.workflow.operation.progress import selection_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.selection import selection_sha256
from baibai_engine.investment_judgment.workflow.workspace import (
    ResearchWorkspaceConflictError,
    prepare_selected_workspace,
)
from baibai_engine.machine_analysis.generation.contracts import GeneratedModel
from baibai_engine.machine_analysis.generation.query import read_research_model_inputs

C = datetime.fromisoformat("2026-10-14T18:00:00+09:00")
T = C + timedelta(minutes=2)


def prepare(app, runs, ws, draft, *, now=T):
    return prepare_selected_workspace(
        draft=draft, db_path=app, runs_path=runs, workspace=ws, now=now
    )


def test_one_runs_snapshot_then_one_app_writer_projects_committed_input(tmp_path, monkeypatch):
    app, runs, ws = tmp_path / "app", tmp_path / "runs", tmp_path / "ws"
    model = GeneratedModel(
        model_id="technical_analysis",
        subject="1234",
        calculation_revision="sample",
        payload={"sample": 1},
    )
    value = seed_generated_selection(runs=runs, now=C, tickers=("1234",), models=(model,))
    draft = generated_draft_for(value).model_copy(
        update={
            "screening_source_sha256": value.inputs.screening_selection.source_sha256,
            "expected_selection_sha256": selection_sha256(value),
        }
    )
    with closing(sqlite3.connect(runs)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"

    calls, readers, writers, changes, models = [], [], [], [], []

    def read_publication(connection, *args, **kwargs):
        assert connection.in_transaction
        readers.append(connection)
        calls.append("publication")
        saved = original_publication(connection, *args, **kwargs)
        # A different connection commits between the two substantive reads.
        with closing(sqlite3.connect(runs)) as writer, writer:
            assert (
                writer.execute(
                    "UPDATE generated_model SET payload_gzip=? WHERE subject='1234'",
                    (gzip.compress(b'{"sample":2}'),),
                ).rowcount
                == 1
            )
        return saved

    def read_models(connection, *args, **kwargs):
        assert readers == [connection]
        assert connection.in_transaction
        calls.append("models")
        result = original_models(connection, *args, **kwargs)
        models.append(weakref.ref(result))
        return result

    def tracked(name, original):
        def read(connection, *args, **kwargs):
            assert connection.in_transaction
            writers.append(connection)
            calls.append(name)
            return original(connection, *args, **kwargs)

        return read

    def open_writer(path):
        connection = original_open(path)
        connection.set_trace_callback(changes.append)
        return connection

    def insert(connection, *args, **kwargs):
        # The source model tree isn't held alongside the committed representation.
        assert [ref() for ref in models] == [None]
        assert all(c is connection for c in writers)
        return original_insert(connection, *args, **kwargs)

    original_publication, original_models = (
        admission.read_case_screening_from_connection,
        admission.read_research_model_inputs_from_connection,
    )
    original_insert, original_open = service._insert, service.connect_existing
    with monkeypatch.context() as m:
        m.setattr(admission, "read_case_screening_from_connection", read_publication)
        m.setattr(admission, "read_research_model_inputs_from_connection", read_models)
        m.setattr(
            admission,
            "latest_assessment_from_connection",
            tracked("market", admission.latest_assessment_from_connection),
        )
        m.setattr(admission, "company_context", tracked("company", admission.company_context))
        m.setattr(
            service,
            "validate_target_purposes",
            tracked("purpose", service.validate_target_purposes),
        )
        m.setattr(service, "connect_existing", open_writer)
        m.setattr(service, "_insert", insert)
        m.setattr(OperationService, "get", lambda *_: pytest.fail("re-read Operation after commit"))
        prepared = prepare(app, runs, ws, draft)
    assert calls == ["publication", "models", "purpose", "market", "company"]
    assert [sql for sql in changes if sql.startswith("BEGIN")] == ["BEGIN IMMEDIATE"]
    assert sum("INSERT INTO operation_session" in sql for sql in changes) == 1
    saved = selection_binding(OperationService(app).get(prepared.operation_id).payload)
    assert saved.inputs.generation.models[0].payload == {"sample": 1}
    assert selection_sha256(saved) == prepared.selection_sha256 == draft.expected_selection_sha256
    latest = read_research_model_inputs(draft.generation_id, targets=("1234",), path=runs)
    assert latest.models[0].payload == {"sample": 2}


def _after_freeze(monkeypatch, action):
    normal = service.freeze_runs

    def freeze(*args):
        result = normal(*args)
        # Independent admission may use the normal owner, without recursive hooks.
        monkeypatch.setattr(service, "freeze_runs", normal)
        action()
        return result

    monkeypatch.setattr(service, "freeze_runs", freeze)


def test_app_active_race_after_real_runs_freeze_refuses_second_writer(tmp_path, monkeypatch):
    app, runs, ws = tmp_path / "app", tmp_path / "runs", tmp_path / "ws"
    value = seed_generated_selection(runs=runs, now=C, tickers=("1234",))
    draft = generated_draft_for(value)
    other = draft.model_copy(update={"selection_summary": "The concurrent independent Case"})
    writer = OperationService(app, runs_path=runs)
    _after_freeze(monkeypatch, lambda: writer.start(other, now=T))
    with pytest.raises(ResearchWorkspaceConflictError, match="active operation already exists"):
        prepare(app, runs, ws, draft)
    (winner,) = writer.list()
    assert (
        selection_binding(writer.get(winner.operation_id).payload).selection_summary
        == other.selection_summary
    )
    assert not ws.exists()


def test_app_purpose_is_current_at_writer_not_static_at_cutoff(tmp_path, monkeypatch):
    app, runs, ws = tmp_path / "app", tmp_path / "runs", tmp_path / "ws"
    # At C the synthetic fill had not occurred; at T it is already reported.
    cutoff = datetime.fromisoformat("2026-06-03T09:59:00+09:00")
    now = cutoff + timedelta(minutes=2)
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    subset = ledger.model_copy(update={"as_of": now, "events": ledger.events[:4]})
    assert cutoff < subset.events[-1].occurred_at <= now
    draft = generated_draft_for(seed_generated_selection(runs=runs, now=cutoff, tickers=("2331",)))
    _after_freeze(monkeypatch, lambda: seed_ledger(app, subset))
    with pytest.raises(ResearchWorkspaceConflictError, match="purpose_conflict"):
        prepare(app, runs, ws, draft, now=now)
    assert OperationService(app).active() is None
    assert not ws.exists()


@pytest.mark.parametrize(
    ("delta", "pin"), [(-1, False), (0, False), (1, False), (0, True), (1, True)]
)
def test_app_publications_racing_freeze_use_actual_cutoff_and_optional_hash(
    tmp_path, monkeypatch, delta, pin
):
    app, runs, ws = tmp_path / "app", tmp_path / "runs", tmp_path / "ws"
    value = seed_generated_selection(runs=runs, now=C, tickers=("1234",))
    draft = generated_draft_for(value).model_copy(
        update={"expected_selection_sha256": selection_sha256(value) if pin else None}
    )
    instant = C.astimezone(UTC) + timedelta(microseconds=delta)
    market = MarketContextAssessment(
        context_id="market-context-assessment-2026-10-14-racing",
        as_of=C.date(),
        known_at=C - timedelta(minutes=2),
        published_at=instant,
        summary="実保存した市場判断",
        inputs=(),
    )
    research = ResearchDraft.model_validate(
        {
            "research_id": "research-racing",
            "document": {
                "ticker": "1234",
                "as_of": C.date().isoformat(),
                "investigated_scope": "時刻の境界",
                "unresolved_questions": ["未解決の問い"],
            },
        }
    )

    def publish():
        AssessmentService(app, clock=lambda: instant).publish(market, expected_head=None)
        ResearchStoreService(app, clock=lambda: instant).save(research)

    _after_freeze(monkeypatch, publish)
    if pin and delta <= 0:
        with pytest.raises(ValueError, match="supplied hash"):
            prepare(app, runs, ws, draft)
        assert OperationService(app).active() is None
        assert not ws.exists()
        return
    final = prepare(app, runs, ws, draft)
    saved = selection_binding(OperationService(app).get(final.operation_id).payload)
    assert saved.input_known_at == C
    context = saved.inputs.saved_company_context["1234"].research
    if delta <= 0:
        assert saved.inputs.saved_market_context_assessment["context_id"] == market.context_id
        assert context.record.recorded_at == instant
        assert context.record.reference.research_id == research.research_id
    else:
        assert saved.inputs.saved_market_context_assessment is None
        assert context.status == "not_found"


@pytest.mark.parametrize(
    ("source", "delta"),
    [
        ("publication", 0),
        ("generation", 0),
        ("publication", 1),
        ("generation", 1),
        ("generation_mismatch", 0),
    ],
)
def test_real_runs_clocks_include_equality_reject_one_microsecond_or_wrong_header(
    tmp_path, source, delta
):
    app, runs, ws = tmp_path / "app", tmp_path / "runs", tmp_path / "ws"
    value = seed_generated_selection(runs=runs, now=C, tickers=("1234",))
    instant = (C.astimezone(UTC) + timedelta(microseconds=delta)).isoformat()
    with closing(sqlite3.connect(runs)) as db, db:
        if source == "publication":
            db.execute("UPDATE screening_selection SET published_at=?", (instant,))
        else:
            header = json.loads(db.execute("SELECT header FROM model_generation").fetchone()[0])
            if source == "generation":
                header["evaluation_time"] = instant
            db.execute(
                "UPDATE model_generation SET evaluation_time=?,header=?",
                (instant, json.dumps(header)),
            )
    draft = generated_draft_for(value)
    if delta or source == "generation_mismatch":
        with pytest.raises(ValueError, match=r"generation|evaluation clock"):
            prepare(app, runs, ws, draft)
        assert not app.exists()
        assert not ws.exists()
    else:
        result = prepare(app, runs, ws, draft)
        saved = selection_binding(OperationService(app).get(result.operation_id).payload)
        clock = (
            saved.inputs.screening_published_at
            if source == "publication"
            else saved.inputs.generation.evaluation_time
        )
        assert clock == C


@pytest.mark.parametrize("clock", ["2026-10-14T18:00:00", "not-a-clock"])
def test_unknown_actual_company_clock_is_not_a_missing_input(tmp_path, clock):
    app, runs, ws = tmp_path / "app", tmp_path / "runs", tmp_path / "ws"
    draft = generated_draft_for(seed_generated_selection(runs=runs, now=C, tickers=("1234",)))
    record = ResearchDraft.model_validate(
        {
            "research_id": "clock",
            "document": {
                "ticker": "1234",
                "as_of": C.date().isoformat(),
                "investigated_scope": "保存時計",
                "unresolved_questions": ["保存時計が既知か"],
            },
        }
    )
    ResearchStoreService(app, clock=lambda: C).save(record)
    with closing(sqlite3.connect(app)) as db, db:
        # A damaged historical/imported header must not reveal the earlier valid revision.
        db.execute(
            "INSERT INTO company_research "
            "(research_id,ticker,as_of,recorded_at,prior_research_id,payload,payload_sha256) "
            "SELECT 'unknown-clock',ticker,as_of,?,research_id,payload,payload_sha256 "
            "FROM company_research WHERE research_id='clock'",
            (clock,),
        )
    with pytest.raises(ValueError, match="unreadable company evidence"):
        prepare(app, runs, ws, draft)
    assert OperationService(app).active() is None
    assert not ws.exists()


@pytest.mark.parametrize("pin", ["screening_source_sha256", "expected_selection_sha256"])
def test_resume_matches_explicit_pins_without_reopening_runs(tmp_path, pin):
    app, runs, ws = tmp_path / "app", tmp_path / "runs", tmp_path / "ws"
    value = seed_generated_selection(runs=runs, now=C, tickers=("1234",))
    draft = generated_draft_for(value)
    result = prepare(app, runs, ws, draft)
    runs.rename(tmp_path / "offline-runs")
    pinned = draft.model_copy(
        update={
            "screening_source_sha256": value.inputs.screening_selection.source_sha256,
            "expected_selection_sha256": result.selection_sha256,
        }
    )
    before = app.read_bytes(), (ws / "domain-inputs.yaml").read_bytes()
    assert prepare(app, runs, ws, pinned).operation_id == result.operation_id
    with pytest.raises(ResearchWorkspaceConflictError, match="different fixed selection"):
        prepare(app, runs, ws, pinned.model_copy(update={pin: "f" * 64}))
    assert before == (app.read_bytes(), (ws / "domain-inputs.yaml").read_bytes())


@pytest.mark.parametrize("delta", [-1, 0, 1, 120_000_000])
def test_prechecked_market_draft_is_fixed_only_after_receipt_at_app_cutoff(
    tmp_path, monkeypatch, delta
):
    from baibai_engine.cli.investment_judgment import main

    app, runs, ws = tmp_path / "app", tmp_path / "runs", tmp_path / "ws"
    value = seed_generated_selection(runs=runs, now=C, tickers=("1234",))
    draft = generated_draft_for(value)
    prior = MarketContextAssessment(
        context_id="market-context-assessment-2026-10-14-in-time",
        as_of=C.date(),
        known_at=C - timedelta(minutes=4),
        published_at=C - timedelta(minutes=2),
        summary="on time",
        inputs=(),
    )
    next_doc = prior.model_copy(
        update={
            "context_id": "market-context-assessment-2026-10-14-declared",
            "published_at": C - timedelta(microseconds=2),
            "summary": "prechecked before receipt",
        }
    )

    def command(document):
        path = tmp_path / (document.context_id + ".json")
        path.write_text(document.model_dump_json())
        return ["market-context", "--db", str(app), "publish", str(path)]

    assert main(command(prior), now=prior.published_at) == 0
    cmd = command(next_doc)
    before = app.read_bytes()
    assert main([*cmd, "--check"], now=next_doc.published_at) == 0
    assert app.read_bytes() == before
    receipt = C.astimezone(UTC) + timedelta(microseconds=delta)

    def publish_after_runs_read():
        assert main([*cmd, "--expected-head", prior.context_id], now=receipt) == 0

    _after_freeze(monkeypatch, publish_after_runs_read)
    op = prepare(app, runs, ws, draft)
    saved = selection_binding(OperationService(app).get(op.operation_id).payload)
    market = saved.inputs.saved_market_context_assessment
    expected = next_doc if delta <= 0 else prior
    assert market["context_id"] == expected.context_id
    assert datetime.fromisoformat(market["published_at"]) == expected.published_at
    assert datetime.fromisoformat(market["recorded_at"]) == (
        receipt if delta <= 0 else prior.published_at
    )


def test_legacy_market_unknown_is_not_pinned_or_treated_as_absent(tmp_path):
    from tests.helpers.assessment_history import seed_history

    app, runs, ws = tmp_path / "app", tmp_path / "runs", tmp_path / "ws"
    value = seed_generated_selection(runs=runs, now=C, tickers=("1234",))
    legacy = MarketContextAssessment(
        context_id="market-context-assessment-2026-10-14-old",
        as_of=C.date(),
        known_at=C - timedelta(minutes=2),
        published_at=C,
        summary="legacy",
        inputs=(),
    )
    seed_history(app, legacy.payload())
    before = app.read_bytes()
    draft = generated_draft_for(value).model_copy(
        update={"expected_selection_sha256": selection_sha256(value)}
    )
    with pytest.raises(ValueError, match="receipt unknown"):
        prepare(app, runs, ws, draft)
    assert OperationService(app).active() is None
    assert app.read_bytes() == before
    assert not ws.exists()
