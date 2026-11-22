"""固定選定、企業評価と独立Review、immutable公開・再開のworkspace結合。"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from tests.helpers.assessment_v1 import (
    assessment_id,
    pair_payload,
    update_review_hash,
)
from tests.helpers.db_seed import seed_ledger
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.cli.investment_judgment import main
from baibai_engine.investment_judgment.contracts import AssessmentError
from baibai_engine.investment_judgment.draft_files import load_draft
from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    assessment_core_hash,
)
from baibai_engine.investment_judgment.stock_assessment.store import (
    AssessmentStoreService,
    load_reviewed_assessment,
)
from baibai_engine.investment_judgment.workflow.operation.progress import progress_binding
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.investment_judgment.workflow.workspace import (
    ResearchWorkspaceConflictError,
    ResearchWorkspaceDataError,
    compute_status,
    prepare_selected_workspace,
    promote,
    resume_selected_workspace,
    scaffold_assessment,
    scaffold_review,
)
from baibai_engine.investment_judgment.workflow.workspace import scaffold as scaffold_module
from baibai_engine.primitives.json import canonical_json

NOW = datetime.fromisoformat("2026-10-14T17:00:00+09:00")


@pytest.fixture
def case(tmp_path):
    db, workspace = tmp_path / "app", tmp_path / "workspace"
    value = seed_generated_selection(runs=tmp_path / "runs", now=NOW, tickers=("1234",))
    result = prepare_selected_workspace(
        draft=generated_draft_for(value),
        db_path=db,
        workspace=workspace,
        runs_path=tmp_path / "runs",
        now=NOW,
    )
    return db, workspace, result.operation_id


def scaffold(case, *, now=NOW, **kwargs):
    db, workspace, _ = case
    return scaffold_assessment(
        workspace=workspace,
        ticker="1234",
        sqlite_path=workspace / "absent",
        as_of=now.date(),
        retrieved_at=now,
        db_path=db,
        **kwargs,
    )


def author(case, *, ticker="1234", workspace=None):
    _db, root, _ = case
    root = workspace or root
    document, review = pair_payload(ticker=ticker)
    directory = root / ticker
    directory.mkdir(exist_ok=True)
    (directory / "assessment-draft.yaml").write_text(yaml.safe_dump(document))
    (directory / "assessment-review.yaml").write_text(yaml.safe_dump(review))
    identity = directory / "assessment-publication.yaml"
    if not identity.exists():
        identity.write_text(
            yaml.safe_dump({"assessment_id": assessment_id(), "supersedes_id": None})
        )
    return document, review


@pytest.mark.parametrize("by_draft", [False, True], ids=["operation-id", "selection-file"])
@pytest.mark.parametrize("force", [False, True])
def test_prepare_requires_root_without_rewriting_a_holding_child(tmp_path, capsys, by_draft, force):
    db, runs, root = tmp_path / "app", tmp_path / "runs", tmp_path / "workspace"
    seed_ledger(
        db, load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    )
    inputs = seed_generated_selection(
        runs=runs, now=NOW, tickers=("2331",), scope="review_positions"
    )
    draft = generated_draft_for(inputs)
    selection_file = tmp_path / "selection.json"
    selection_file.write_text(draft.model_dump_json(), encoding="utf-8")
    prepared = prepare_selected_workspace(
        draft=draft, db_path=db, workspace=root, runs_path=runs, now=NOW
    )
    child = root / "holdings/2331"
    author((db, child, prepared.operation_id), ticker="2331")
    files = {p: p.read_bytes() for p in root.rglob("*.yaml")}
    input_bytes = db.read_bytes(), runs.read_bytes(), selection_file.read_bytes()
    before = compute_status(child, db_path=db, now=NOW)
    assert before["cases"][0]["status"] == "ready_for_promotion"
    source = (
        ["--selection-file", str(selection_file)]
        if by_draft
        else ["--operation-id", prepared.operation_id]
    )
    args = ["prepare", *source, "--db", str(db), "--runs-path", str(runs)]
    flags = ["--force"] if force else []
    assert main([*args, "--workspace", str(child), *flags], now=NOW) == 4
    refused = capsys.readouterr()
    assert refused.out == ""
    assert "root workspace" in refused.err
    assert {p: p.read_bytes() for p in root.rglob("*.yaml")} == files
    assert not (child / "holdings").exists()
    assert (db.read_bytes(), runs.read_bytes(), selection_file.read_bytes()) == input_bytes

    assert main([*args, "--workspace", str(root), *flags], now=NOW) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["status"] == "resumed"
    assert resumed["operation_id"] == prepared.operation_id
    assert resumed["selection_sha256"] == prepared.selection_sha256
    assert compute_status(child, db_path=db, now=NOW) == before
    assert {p: p.read_bytes() for p in root.rglob("*.yaml")} == files
    assert (db.read_bytes(), runs.read_bytes(), selection_file.read_bytes()) == input_bytes

    assert main(_promote_command(db, child, "2331"), now=NOW) == 0
    assert compute_status(child, db_path=db, now=NOW)["cases"][0]["status"] == "published"


def test_initial_scaffold_has_new_contract_and_stable_writer_identity(case):
    first = scaffold(case)
    text = Path(first["assessment_draft"]).read_text()
    raw = yaml.safe_load(text)
    assert "docs/reference/model-observations.md#public-research-reports" in text
    assert "# 元Researchの調査範囲" not in text
    reference = Path(__file__).resolve().parents[4] / "docs/reference/model-observations.md"
    assert '<a id="public-research-reports"></a>' in reference.read_text()
    assert raw["kind"] == "stock_assessment"
    assert raw["schema_version"] == 1
    drafts = raw["model_perspectives"]
    assert len({r["model"] for r in drafts}) == len(drafts) == 7
    assert all(
        r["assessment"] is None and r["summary"] is None and not r["source_ids"] for r in drafts
    )
    assert raw["findings"]["business_understanding"] is None
    assert raw["valuation"]["horizon_months"] is None
    assert raw["valuation"]["required_annual_return_pct"] is None
    assert "judgment" not in raw
    assert first["assessment_id"].startswith("stock-assessment-")
    with pytest.raises(ValidationError):
        StockAssessmentDocument.model_validate(raw)
    with pytest.raises(ResearchWorkspaceConflictError, match="exists"):
        scaffold(case)
    assert scaffold(case, force=True)["assessment_id"] == first["assessment_id"]


def test_scaffold_out_exports_same_payload_and_preserves_canonical_writer_source(case):
    db, workspace, _ = case
    exported = workspace.parent / "custom-draft.yaml"
    result = scaffold(case, out=exported)
    canonical = workspace / "1234/assessment-draft.yaml"
    assert exported.read_bytes() == canonical.read_bytes()
    assert result["exported_draft"] == str(exported)
    exported.write_text("independently edited export")
    assert yaml.safe_load(canonical.read_text())["kind"] == "stock_assessment"
    assert (
        yaml.safe_load((canonical.parent / "assessment-publication.yaml").read_text())[
            "assessment_id"
        ]
        == result["assessment_id"]
    )
    author(case)
    promoted = promote(workspace=workspace, ticker="1234", db_path=db, now=NOW)
    assert promoted.assessment_id == result["assessment_id"]


def test_scaffold_export_collision_has_no_workspace_write(case):
    _db, workspace, _ = case
    exported = workspace.parent / "custom-draft.yaml"
    exported.write_text("existing authored file")
    with pytest.raises(ResearchWorkspaceConflictError, match="export exists"):
        scaffold(case, out=exported)
    assert not (workspace / "1234").exists()
    assert exported.read_text() == "existing authored file"


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("alias", [False, True])
def test_scaffold_export_cannot_replace_its_new_publication_identity(case, force, alias):
    _db, workspace, _ = case
    exported = workspace / "1234/assessment-publication.yaml"
    if alias:
        linked = workspace.parent / "identity-export.yaml"
        linked.symlink_to(exported)
        exported = linked
    with pytest.raises(ResearchWorkspaceConflictError, match="publication identity"):
        scaffold(case, out=exported, force=force)
    assert not (workspace / "1234").exists()


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), 0.0, -1.0, "bad"])
def test_invalid_stored_price_remains_unresolved_in_draft(case, invalid):
    from tests.helpers.screening_sqlite import seed_daily_bars

    db, workspace, _ = case
    market = workspace / "market.sqlite"
    seed_daily_bars(market, [("1234", NOW.date().isoformat(), 100.0, 1.0)])
    with closing(sqlite3.connect(market)) as connection, connection:
        connection.execute("UPDATE jquants_daily_bars SET close = ?", (invalid,))
    result = scaffold_assessment(
        workspace=workspace,
        ticker="1234",
        sqlite_path=market,
        as_of=NOW.date(),
        retrieved_at=NOW,
        db_path=db,
    )
    draft = yaml.safe_load(Path(result["assessment_draft"]).read_text())
    assert result["close_yen"] is None
    assert draft["input_snapshot"]["facts"] == []
    assert draft["valuation"]["market_price_fact_id"] is None
    assert draft["valuation"]["status"] == "unresolved"


@pytest.mark.parametrize(
    ("as_of", "retrieved_at", "expected_date", "expected_price"),
    [
        ("2026-10-14", "2026-10-15T17:00:00+09:00", "2026-10-14", 100.0),
        ("2026-10-15", "2026-10-15T09:00:00+09:00", "2026-10-14", 100.0),
        ("2026-10-15", "2026-10-15T17:00:00+09:00", "2026-10-15", 200.0),
    ],
)
def test_scaffold_quote_respects_assessment_date_and_available_close(
    case, as_of, retrieved_at, expected_date, expected_price
):
    from tests.helpers.screening_sqlite import seed_daily_bars

    db, workspace, _ = case
    market = workspace / "market.sqlite"
    seed_daily_bars(
        market,
        [("1234", "2026-10-14", 100.0, 1.0), ("1234", "2026-10-15", 200.0, 1.0)],
    )
    result = scaffold_assessment(
        workspace=workspace,
        ticker="1234",
        sqlite_path=market,
        as_of=datetime.fromisoformat(as_of).date(),
        retrieved_at=datetime.fromisoformat(retrieved_at),
        db_path=db,
    )
    raw = yaml.safe_load(Path(result["assessment_draft"]).read_text())
    assert result["price_as_of"] == expected_date
    assert result["close_yen"] == expected_price
    assert raw["input_snapshot"]["facts"][0]["as_of"] == expected_date
    assert raw["input_snapshot"]["sources"][0]["retrieved_at"] == retrieved_at


def test_review_scaffold_has_exact_hash_new_uuid_and_independent_empty_slots(case, monkeypatch):
    db, workspace, _ = case
    author(case)
    (workspace / "1234/assessment-review.yaml").unlink()
    reads = []

    def read_once(path, model, *, label):
        reads.append(path)
        return load_draft(path, model, label=label)

    monkeypatch.setattr(scaffold_module, "load_draft", read_once)
    result = scaffold_review(workspace=workspace, ticker="1234", db_path=db)
    assert reads == [workspace / "1234/assessment-draft.yaml"]
    raw = yaml.safe_load(Path(result["review_draft"]).read_text())
    assert Path(result["review_draft"]).name == "assessment-review.yaml"
    assert raw["review_id"].startswith("assessment-review-")
    assert raw["reviewed_assessment_sha256"] == result["reviewed_assessment_sha256"]
    assert raw["review_scope"] is None
    assert raw["checked_source_ids"] == []
    assert [row["terminal_value_per_share_yen"] for row in raw["recalculated_projections"]] == [
        None,
        None,
    ]


def test_promote_records_exact_assessment_for_new_target_and_retry_preserves_id(case):
    db, workspace, operation_id = case
    scaffold(case)
    author(case)
    inputs = (workspace / "domain-inputs.yaml").read_bytes()
    first = promote(workspace=workspace, ticker="1234", db_path=db, now=NOW)
    assert (
        promote(workspace=workspace, ticker="1234", db_path=db, now=NOW + timedelta(days=1))
        == first
    )
    progress = progress_binding(OperationService(db).get(operation_id).payload)
    assert progress.outcomes[0].assessment_ref.assessment_id == first.assessment_id
    assert progress.outcomes[0].purpose == "new"
    status = compute_status(workspace, db_path=db, now=NOW + timedelta(days=2))
    assert status["cases"][0]["assessment_id"] == first.assessment_id
    assert status["cases"][0]["status"] == "published"
    assert (workspace / "domain-inputs.yaml").read_bytes() == inputs
    with closing(sqlite3.connect(db)) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM stock_assessment").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ledger_event").fetchone()[0] == 0


def _before_promotion_write(monkeypatch, db, action):
    """Inject once on either App writer, never on the read-only preflight connection."""
    connect = sqlite3.connect

    def interleave(database, *args, **kwargs):
        if str(database) in (str(db), db.resolve().as_uri() + "?mode=rw"):
            monkeypatch.setattr(sqlite3, "connect", connect)
            action()
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", interleave)


def _promote_command(db, workspace, ticker="1234"):
    return [
        "stock-assessment-promote",
        "--workspace",
        str(workspace),
        "--db",
        str(db),
        "--ticker",
        ticker,
    ]


def _revised_workspace(tmp_path):
    from tests.helpers.assessment_v1 import review_id
    from tests.helpers.proposal import PUBLISHED, backend_case, reviewed_proposal

    from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService

    prepared = backend_case(tmp_path)
    db, operation_id = prepared.app, prepared.operation.operation_id
    proposal = TradeProposalService(
        db, sqlite_path=prepared.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(prepared, action="no_action"))
    workspace = tmp_path / "workspace"
    resume_selected_workspace(operation_id=operation_id, workspace=workspace, db_path=db, now=NOW)
    doc, review = author((db, workspace, operation_id))
    doc["findings"]["business_understanding"] += "新根拠を確認した独立の評価"
    review["review_id"] = review_id(10)
    update_review_hash(doc, review)
    new_id = assessment_id(9)
    pair_dir = workspace / "1234"
    (pair_dir / "assessment-draft.yaml").write_text(yaml.safe_dump(doc))
    (pair_dir / "assessment-review.yaml").write_text(yaml.safe_dump(review))
    (pair_dir / "assessment-publication.yaml").write_text(
        yaml.safe_dump({"assessment_id": new_id, "supersedes_id": prepared.pair.assessment_id})
    )
    return prepared, workspace, proposal, new_id


@pytest.mark.parametrize("completion_phase", ["before_publication", "before_binding"])
def test_promote_keeps_completion_on_the_correct_side_of_publication(
    tmp_path, monkeypatch, capsys, completion_phase
):
    from tests.helpers.proposal import PUBLISHED

    from baibai_engine.investment_judgment.stock_assessment.store import latest_assessment_id

    prepared, workspace, proposal, new_id = _revised_workspace(tmp_path)
    db, operation_id = prepared.app, prepared.operation.operation_id
    original_id = prepared.pair.assessment_id
    now = PUBLISHED + timedelta(minutes=2)

    def complete():
        done = prepared.operations.complete(
            operation_id,
            proposal_id=proposal.proposal_id,
            result="従前の評価と提案で終了",
            next="人間の判断待ち",
            completed_at=PUBLISHED + timedelta(minutes=1),
        )
        assert done.status == "completed"

    if completion_phase == "before_publication":
        _before_promotion_write(monkeypatch, db, complete)
    else:
        bind = OperationService.record_assessment

        def complete_before_bind(self, *args, **kwargs):
            # An exact pair must already be readable through another connection.
            with closing(sqlite3.connect(db)) as connection:
                assert latest_assessment_id(connection, "1234") == new_id
            complete()
            return bind(self, *args, **kwargs)

        monkeypatch.setattr(OperationService, "record_assessment", complete_before_bind)

    assert main(_promote_command(db, workspace), now=now) == 4
    result = capsys.readouterr()
    operation = prepared.operations.get(operation_id)
    assert operation.status == "completed"
    assert (
        progress_binding(operation.payload).outcomes[0].assessment_ref.assessment_id == original_id
    )
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT count(*) FROM ledger_event").fetchone()[0] == 1
        if completion_phase == "before_publication":
            assert latest_assessment_id(connection, "1234") == original_id
            assert connection.execute("SELECT count(*) FROM stock_assessment").fetchone()[0] == 1
            assert (
                connection.execute("SELECT count(*) FROM stock_assessment_review").fetchone()[0]
                == 1
            )
            assert result.out == ""
            assert "completed research Operation" in result.err
        else:
            assert latest_assessment_id(connection, "1234") == new_id
            pair = load_reviewed_assessment(connection, new_id)
            assert connection.execute("SELECT count(*) FROM stock_assessment").fetchone()[0] == 2
            assert (
                connection.execute("SELECT count(*) FROM stock_assessment_review").fetchone()[0]
                == 2
            )
            assert yaml.safe_load(result.out) == {
                "status": "published_binding_unconfirmed",
                "operation_id": operation_id,
                "operation_status": "completed",
                "assessment_id": new_id,
                "review_id": pair.review.review_id,
                "assessment_sha256": pair.core_sha256,
            }
            assert "completed operation session is immutable" in result.err
            (status,) = compute_status(workspace, db_path=db, now=now)["cases"]
            assert status["status"] == "published_not_bound"
            assert status["next_action"] is None


def test_promote_does_not_publish_for_a_holding_sold_after_preflight(tmp_path, monkeypatch, capsys):
    from baibai_engine.portfolio.ledger.drafts import apply_draft, build_sell_execution_draft
    from baibai_engine.portfolio.ledger.store import LedgerStoreService

    db, runs = tmp_path / "app", tmp_path / "runs"
    seed_ledger(
        db, load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    )
    selected = seed_generated_selection(
        runs=runs, now=NOW, tickers=("2331",), scope="review_positions"
    )
    prepared = prepare_selected_workspace(
        draft=generated_draft_for(selected),
        workspace=tmp_path / "workspace",
        runs_path=runs,
        db_path=db,
        now=NOW,
    )
    child = prepared.workspace / "holdings/2331"
    author((db, child, prepared.operation_id), ticker="2331")
    later = NOW + timedelta(days=1)
    assert main(["status", "--workspace", str(child), "--db", str(db)], now=later) == 0
    assert yaml.safe_load(capsys.readouterr().out)["cases"][0]["status"] == "ready_for_promotion"
    ledger = LedgerStoreService(db)

    def sell():
        # This is the real finite ledger transition over a representative synthetic holding.
        sale = build_sell_execution_draft(
            ledger, occurred_at=later, ticker="2331", quantity=200, price_yen=Decimal("1100")
        )
        assert sale is not None
        apply_draft(ledger, sale, human_confirmed=True)

    _before_promotion_write(monkeypatch, db, sell)
    assert main(_promote_command(db, child, "2331"), now=later) == 4
    result = capsys.readouterr()
    assert result.out == ""
    assert "holding workspace requires a current holding" in result.err
    operation = OperationService(db).get(prepared.operation_id)
    assert operation.status == "active"
    assert not progress_binding(operation.payload).outcomes
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT count(*) FROM stock_assessment").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM stock_assessment_review").fetchone()[0] == 0
    assert ledger.load().events[-1].ticker == "2331"


def test_promote_acknowledges_exact_publication_and_explicit_binding_recovers_after_cas(
    case, monkeypatch, capsys
):
    from baibai_engine.cli.operation import main as operation_main
    from baibai_engine.investment_judgment.workflow.operation import service as operation_module

    db, workspace, operation_id = case
    document, review = author(case)
    operations = OperationService(db)
    write = operation_module.connect_existing

    def checkpoint_before_bind_writer(path=None):
        monkeypatch.setattr(operation_module, "connect_existing", write)
        operations.checkpoint(operation_id, note="別工程が判断メモを追加", now=NOW)
        return write(path)

    monkeypatch.setattr(operation_module, "connect_existing", checkpoint_before_bind_writer)
    assert main(_promote_command(db, workspace), now=NOW) == 4
    issue = capsys.readouterr()
    assert yaml.safe_load(issue.out) == {
        "status": "published_binding_unconfirmed",
        "operation_id": operation_id,
        "operation_status": "active",
        "assessment_id": assessment_id(),
        "review_id": review["review_id"],
        "assessment_sha256": review["reviewed_assessment_sha256"],
    }
    assert "research progress changed" in issue.err

    def saved_rows():
        with closing(sqlite3.connect(db)) as connection:
            pair = connection.execute("SELECT * FROM stock_assessment").fetchall()
            review_rows = connection.execute("SELECT * FROM stock_assessment_review").fetchall()
            ledger = connection.execute("SELECT * FROM ledger_event").fetchall()
        return pair, review_rows, ledger

    before = saved_rows()
    assert len(before[0]) == len(before[1]) == 1
    assert not before[2]
    assert not progress_binding(operations.get(operation_id).payload).outcomes
    assert main(["status", "--workspace", str(workspace), "--db", str(db)], now=NOW) == 0
    local = yaml.safe_load(capsys.readouterr().out)
    assert local["cases"][0]["status"] == "published_not_bound"
    assert "record-assessment" in local["next_action"]
    assert (
        operation_main(
            [
                "--db",
                str(db),
                "--format",
                "json",
                "record-assessment",
                operation_id,
                "--assessment-id",
                assessment_id(),
            ],
            now=NOW,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "active"
    loaded = operations.get(operation_id)
    assert loaded.payload.checkpoint == "別工程が判断メモを追加"
    (outcome,) = progress_binding(loaded.payload).outcomes
    assert outcome.assessment_ref.core_sha256 == review["reviewed_assessment_sha256"]
    assert outcome.assessment_ref.review_id == review["review_id"]
    with closing(sqlite3.connect(db)) as connection:
        assert (
            load_reviewed_assessment(connection, assessment_id()).document.model_dump(mode="json")
            == document
        )
    assert saved_rows() == before


def test_promote_retains_proposal_conflict_exit_and_reports_exact_saved_pair(tmp_path, capsys):
    from tests.helpers.proposal import PUBLISHED

    from baibai_engine.investment_judgment.workflow.operation.progress import (
        ProposalRef,
        with_progress,
    )
    from baibai_engine.storage.sqlite.application.write import connect_existing

    prepared, workspace, proposal, new_id = _revised_workspace(tmp_path)
    db, operation_id = prepared.app, prepared.operation.operation_id
    operations = prepared.operations
    old = operations.get(operation_id)
    retained_id = "investment-proposal-00000000-0000-4000-8000-000000000098"
    progress = progress_binding(old.payload).model_copy(
        update={
            "proposal_ref": ProposalRef(
                proposal_id=retained_id, draft_sha256=proposal.review.draft_sha256
            )
        }
    )
    with closing(connect_existing(db)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        # A retained active Operation can have a prior Proposal reference. Only this
        # fixture's copied row is inconsistent; the normal proposal/pair writer is unchanged.
        connection.execute(
            "UPDATE operation_session SET payload=? WHERE operation_id=?",
            (with_progress(old.payload, progress).model_dump_json(), operation_id),
        )
        connection.execute(
            "INSERT INTO investment_proposal "
            "(proposal_id,as_of,prepared_at,published_at,request_scope,payload,draft_sha256) "
            "SELECT ?,as_of,prepared_at,published_at,request_scope,payload,draft_sha256 "
            "FROM investment_proposal WHERE proposal_id=?",
            (retained_id, proposal.proposal_id),
        )
    unchanged = operations.get(operation_id)
    assert main(_promote_command(db, workspace), now=PUBLISHED + timedelta(minutes=2)) == 4
    result = capsys.readouterr()
    assert "stored proposal identity/hash differs from its payload" in result.err
    assert operations.get(operation_id) == unchanged
    with closing(sqlite3.connect(db)) as connection:
        pair = load_reviewed_assessment(connection, new_id)
        assert load_reviewed_assessment(connection, prepared.pair.assessment_id) == prepared.pair
    assert yaml.safe_load(result.out) == {
        "status": "published_binding_unconfirmed",
        "operation_id": operation_id,
        "operation_status": "active",
        "assessment_id": pair.assessment_id,
        "review_id": pair.review.review_id,
        "assessment_sha256": pair.core_sha256,
    }


@pytest.mark.parametrize("status_available", [True, False])
def test_promote_does_not_guess_that_a_lost_bind_ack_means_unbound(
    case, monkeypatch, capsys, status_available
):
    db, workspace, operation_id = case
    _, review = author(case)
    record = OperationService.record_assessment
    get = OperationService.get
    bound = False

    def lose_ack_after_commit(service, *args, **kwargs):
        nonlocal bound
        record(service, *args, **kwargs)
        bound = True
        raise OSError("simulated failure after the actual Operation bind")

    def read_operation_status(service, *args, **kwargs):
        if bound and not status_available:
            raise sqlite3.OperationalError("simulated status read failure")
        return get(service, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(OperationService, "record_assessment", lose_ack_after_commit)
        fault.setattr(OperationService, "get", read_operation_status)
        assert main(_promote_command(db, workspace), now=NOW) == 3
    result = capsys.readouterr()
    assert bound
    assert "simulated failure after the actual Operation bind" in result.err
    assert yaml.safe_load(result.out) == {
        "status": "published_binding_unconfirmed",
        "operation_id": operation_id,
        "operation_status": "active" if status_available else None,
        "assessment_id": assessment_id(),
        "review_id": review["review_id"],
        "assessment_sha256": review["reviewed_assessment_sha256"],
    }

    state = compute_status(workspace, db_path=db, now=NOW)
    (case_status,) = state["cases"]
    assert case_status["status"] == "published"
    assert case_status["assessment_id"] == assessment_id()
    assert state["next_action"] is None
    database = db.read_bytes()
    OperationService(db).record_assessment(
        operation_id, assessment_id(), now=NOW + timedelta(minutes=1)
    )
    assert db.read_bytes() == database


def test_clean_resume_reads_only_the_bound_exact_pair_not_the_same_company_head(case, tmp_path):
    db, _, operation_id = case
    store = AssessmentStoreService(db, clock=lambda: NOW)
    old, review = pair_payload()
    bound = store.publish_reviewed_assessment(
        assessment_payload=old,
        review_payload=review,
        assessment_id=assessment_id(),
        supersedes_id=None,
    )
    resume = tmp_path / "second-workspace"
    resume_selected_workspace(operation_id=operation_id, workspace=resume, db_path=db, now=NOW)
    # A publication about the same company, even today, is not the Operation's chosen pair.
    assert compute_status(resume, db_path=db, now=NOW)["cases"][0]["status"] == "incomplete"
    OperationService(db).record_assessment(operation_id, bound.assessment_id, now=NOW)
    other, other_review = pair_payload(review_number=3)
    other["findings"]["business_understanding"] += "Separate decision premise"
    update_review_hash(other, other_review)
    AssessmentStoreService(
        db, clock=lambda: NOW + timedelta(seconds=5)
    ).publish_reviewed_assessment(
        assessment_payload=other,
        review_payload=other_review,
        assessment_id=assessment_id(2),
        supersedes_id=bound.assessment_id,
    )
    after = db.read_bytes()
    assert not (resume / "1234").exists()
    status = compute_status(resume, db_path=db, now=NOW + timedelta(days=1))
    (row,) = status["cases"]
    assert status["workspace_status"] == row["status"] == "published"
    assert status["next_action"] is row["next_action"] is None
    assert row["assessment_id"] == bound.assessment_id
    assert row["assessment_as_of"] == NOW.date().isoformat()  # Not tomorrow's evaluation.
    assert row["recorded_outcome"]["assessment_ref"] == {
        "assessment_id": bound.assessment_id,
        "core_sha256": bound.core_sha256,
        "review_id": bound.review.review_id,
    }
    assert not (resume / "1234").exists()
    assert db.read_bytes() == after

    # The other published pair has now also been opened here. It did not change the decision.
    directory = case[1] / "1234"
    directory.mkdir(exist_ok=True)
    (directory / "assessment-draft.yaml").write_text(yaml.safe_dump(other))
    (directory / "assessment-review.yaml").write_text(yaml.safe_dump(other_review))
    local = compute_status(case[1], db_path=db, now=NOW + timedelta(hours=1))
    (local_row,) = local["cases"]
    assert local["workspace_status"] == "incomplete"
    assert local_row["status"] == "published_not_bound"
    assert local_row["status_basis"] == "workspace_files"
    assert local_row["assessment_id"] == assessment_id(2)
    assert local_row["recorded_outcome"]["assessment_ref"]["assessment_id"] == bound.assessment_id
    assert "record-assessment" in local["next_action"]
    assert db.read_bytes() == after

    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("DROP TRIGGER stock_assessment_immutable")  # Corrupted fixture copy.
        conn.execute(
            "UPDATE stock_assessment SET core_sha256 = ? WHERE assessment_id = ?",
            ("0" * 64, bound.assessment_id),
        )
    (row,) = compute_status(resume, db_path=db, now=NOW + timedelta(days=1))["cases"]
    assert row["status"] == "recorded_outcome_unreadable"
    assert row["assessment_id"] is None
    assert row["assessment_validation_errors"]
    assert "verify" in row["next_action"]  # Do not substitute the healthy newer pair.


def test_clean_resume_distinguishes_unfinished_from_unresearched_without_hiding_local_edits(
    case, tmp_path
):
    db, _, operation_id = case
    OperationService(db).record_unfinished(
        operation_id,
        ticker="1234",
        reason="決算延期。訂正後の事業キャッシュが不明。",
        references=("決算延期の公表資料",),
        now=NOW,
    )
    resume = tmp_path / "second-workspace"
    resume_selected_workspace(operation_id=operation_id, workspace=resume, db_path=db, now=NOW)
    before = db.read_bytes()
    status = compute_status(resume, db_path=db, now=NOW)
    (row,) = status["cases"]
    assert status["workspace_status"] == "incomplete"  # Explicitly unfinished is not published.
    assert row["status"] == "recorded_unfinished"
    assert row["assessment_id"] is None
    assert row["recorded_outcome"]["unfinished_reason"].startswith("決算延期")
    assert row["recorded_outcome"]["references"] == ["決算延期の公表資料"]
    assert row["next_action"] is status["next_action"] is None

    # A started or broken local draft is not an absent draft.
    path = resume / "1234" / "assessment-draft.yaml"
    path.parent.mkdir()
    path.write_text("{}")
    (row,) = compute_status(resume, db_path=db, now=NOW)["cases"]
    assert row["status"] == "incomplete"
    assert row["assessment_validation_errors"]
    assert row["next_action"]
    assert db.read_bytes() == before

    # Even both files of a valid publication do not replace the explicit unfinished result.
    doc, review = author((db, resume, operation_id))
    AssessmentStoreService(db, clock=lambda: NOW).publish_reviewed_assessment(
        assessment_id(), doc, review
    )
    saved = db.read_bytes()
    active = compute_status(resume, db_path=db, now=NOW)
    (published,) = active["cases"]
    assert active["workspace_status"] == "incomplete"
    assert published["status"] == "published_not_bound"
    assert published["recorded_outcome"]["status"] == "unfinished"
    assert published["assessment_id"] == assessment_id()
    assert "record-assessment" in active["next_action"]
    assert db.read_bytes() == saved


def test_legacy_price_clock_pair_remains_readable_and_retryable_but_not_newly_publishable(
    case, capsys
):
    db, workspace, _ = case
    scaffold(case)
    document, review = author(case)
    document["input_snapshot"]["facts"][0]["price_basis"] = "realtime"
    document["input_snapshot"]["facts"][0]["observed_at"] = "2026-10-14T14:00:00+09:00"
    next(
        source for source in document["input_snapshot"]["sources"] if source["source_id"] == "price"
    )["retrieved_at"] = "2026-10-14T10:00:00+09:00"
    update_review_hash(document, review)
    identity_path = workspace / "1234/assessment-publication.yaml"
    identity = yaml.safe_load(identity_path.read_text())
    saved_id = identity["assessment_id"]
    draft_path = workspace / "1234/assessment-draft.yaml"
    review_path = workspace / "1234/assessment-review.yaml"
    draft_path.write_text(yaml.safe_dump(document))
    review_path.write_text(yaml.safe_dump(review))
    core_hash = assessment_core_hash(StockAssessmentDocument.model_validate(document))
    # Fixed historical rows: the prior authoring check accepted this exact pair.
    # Do not use the new writer to assert that legacy content is newly publishable.
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute(
            "INSERT INTO stock_assessment(assessment_id,ticker,as_of,stance,published_at,"
            "supersedes_id,payload,core_sha256) VALUES (?,?,?,?,?,?,?,?)",
            (
                saved_id,
                "1234",
                "2026-10-14",
                document["conclusion"]["stance"],
                NOW.isoformat(),
                None,
                canonical_json(document),
                core_hash,
            ),
        )
        connection.execute(
            "INSERT INTO stock_assessment_review(review_id,assessment_id,reviewed_at,payload) "
            "VALUES (?,?,?,?)",
            (review["review_id"], saved_id, review["reviewed_at"], canonical_json(review)),
        )
        saved = load_reviewed_assessment(connection, saved_id)
        assert saved.core_sha256 == core_hash
        assert saved.document.model_dump(mode="json") == document
    assert (
        AssessmentStoreService(db, clock=lambda: NOW).publish_reviewed_assessment(
            saved_id, document, review
        )
        == saved
    )
    assert (
        compute_status(workspace, db_path=db, now=NOW)["cases"][0]["status"]
        == "published_not_bound"
    )
    promoted = promote(workspace=workspace, ticker="1234", db_path=db, now=NOW)
    assert promote(workspace=workspace, ticker="1234", db_path=db, now=NOW) == promoted
    assert promoted.assessment_sha256 == core_hash
    bound_database = db.read_bytes()
    assert main(_promote_command(db, workspace), now=NOW + timedelta(days=1)) == 0
    result = capsys.readouterr()
    assert result.err == ""
    assert yaml.safe_load(result.out) == {
        "assessment_id": saved_id,
        "review_id": saved.review.review_id,
        "assessment_sha256": core_hash,
    }
    assert db.read_bytes() == bound_database
    identity_path.write_text(
        yaml.safe_dump({"assessment_id": assessment_id(99), "supersedes_id": saved_id})
    )
    with pytest.raises(ResearchWorkspaceDataError, match="postdates all cited source retrievals"):
        promote(workspace=workspace, ticker="1234", db_path=db, now=NOW)
    with closing(sqlite3.connect(db)) as connection:
        assert load_reviewed_assessment(connection, saved_id) == saved
        assert connection.execute("SELECT COUNT(*) FROM stock_assessment").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ledger_event").fetchone()[0] == 0


def test_status_requires_exact_pair_and_no_stale_review_promotion(case):
    db, workspace, _ = case
    scaffold(case)
    document, review = author(case)
    promote(workspace=workspace, ticker="1234", db_path=db, now=NOW)
    document["findings"]["business_understanding"] += "新しい調査文"
    draft = workspace / "1234/assessment-draft.yaml"
    draft.write_text(yaml.safe_dump(document))
    status = compute_status(workspace, db_path=db, now=NOW)
    assert status["cases"][0]["status"] == "ready_for_review"
    assert status["cases"][0]["assessment_id"] is None
    with pytest.raises(ResearchWorkspaceDataError, match="stale"):
        promote(workspace=workspace, ticker="1234", db_path=db, now=NOW)
    update_review_hash(document, review)
    (workspace / "1234/assessment-review.yaml").write_text(yaml.safe_dump(review))
    assert (
        compute_status(workspace, db_path=db, now=NOW)["cases"][0]["status"]
        == "ready_for_promotion"
    )
    with pytest.raises(ResearchWorkspaceConflictError, match="immutable"):
        promote(workspace=workspace, ticker="1234", db_path=db, now=NOW)


def test_refresh_scaffold_preserves_sources_and_requires_new_horizon_and_review(case, monkeypatch):
    db, workspace, _ = case
    scaffold(case)
    old, _ = author(case)
    published = promote(workspace=workspace, ticker="1234", db_path=db, now=NOW)
    monkeypatch.setattr(
        scaffold_module, "read_unadjusted_close", lambda **_kw: pytest.fail("refresh read quote")
    )
    refreshed = scaffold(
        case, now=NOW + timedelta(days=1), force=True, from_assessment_id=published.assessment_id
    )
    raw = yaml.safe_load(Path(refreshed["assessment_draft"]).read_text())
    assert refreshed["assessment_id"] != published.assessment_id
    assert raw["input_snapshot"]["sources"] == old["input_snapshot"]["sources"]
    assert raw["input_snapshot"]["facts"] == old["input_snapshot"]["facts"]
    assert raw["valuation"]["horizon_months"] is None
    assert raw["valuation"]["base"]["cash_distribution_per_share_yen"] is None
    assert raw["refresh"]["checked_at"] is None
    assert raw["refresh"]["horizon_reason"] is None
    assert raw["refresh"]["prior_assessment_ref"]["assessment_id"] == published.assessment_id
    with pytest.raises(ResearchWorkspaceDataError):
        promote(workspace=workspace, ticker="1234", db_path=db, now=NOW + timedelta(days=1))


def _prior_and_latest(case):
    db, _workspace, _operation = case
    prior, review = pair_payload(review_number=12)
    first = AssessmentStoreService(
        db, clock=lambda: datetime.fromisoformat("2026-10-14T16:06:00+09:00")
    ).publish_reviewed_assessment(assessment_id(11), prior, review)
    latest, review = pair_payload(review_number=14)
    latest["findings"]["business_understanding"] += " Bで検討した補足。"
    latest["conclusion"]["assessed_at"] = "2026-10-14T16:08:00+09:00"
    review["reviewed_at"] = "2026-10-14T16:13:00+09:00"
    update_review_hash(latest, review)
    second = AssessmentStoreService(
        db, clock=lambda: datetime.fromisoformat("2026-10-14T16:16:00+09:00")
    ).publish_reviewed_assessment(
        assessment_id(13), latest, review, supersedes_id=first.assessment_id
    )
    return first, second


def test_refresh_from_old_material_supersedes_latest_and_publishes_new_reviewed_pair(case):
    db, workspace, _ = case
    prior, latest = _prior_and_latest(case)
    result = scaffold(case, from_assessment_id=prior.assessment_id)
    draft_path = Path(result["assessment_draft"])
    raw = yaml.safe_load(draft_path.read_text())
    identity = yaml.safe_load((draft_path.parent / "assessment-publication.yaml").read_text())
    assert raw["refresh"]["prior_assessment_ref"] == {
        "assessment_id": prior.assessment_id,
        "core_sha256": prior.core_sha256,
        "review_id": prior.review.review_id,
    }
    assert identity["supersedes_id"] == latest.assessment_id
    assert (
        raw["input_snapshot"]["sources"]
        == prior.document.model_dump(mode="json")["input_snapshot"]["sources"]
    )
    assert (
        raw["input_snapshot"]["facts"]
        == prior.document.model_dump(mode="json")["input_snapshot"]["facts"]
    )
    raw["price_value_case"] = prior.document.price_value_case.model_dump(mode="json")
    raw["valuation"]["horizon_months"] = 12
    for name in ("base", "downside"):
        raw["valuation"][name]["cash_distribution_per_share_yen"] = "0"
    raw["refresh"].update(
        checked_at=NOW.isoformat(),
        material_information_check="Aの保存資料と最新Bの差分を確認し、今回の所見を再検討した。",
        changed_sections=["findings"],
        unchanged_assumptions_reason="継続収益の仮定を原資料で再確認した。",
        horizon_reason="本日起点の約12ヶ月として再確認した。",
        distribution_reason="今回期間に受け取る分配0を再確認した。",
        corporate_action_reason="起点の株式単位に変更なし。",
    )
    raw["findings"]["value_gap_explanation"] += " 最新Bの補足を踏まえて原Aの価値差を再確認。"
    draft_path.write_text(yaml.safe_dump(raw))
    review_result = scaffold_review(workspace=workspace, ticker="1234", db_path=db)
    review_path = Path(review_result["review_draft"])
    review = yaml.safe_load(review_path.read_text())
    review.update(
        reviewer_identity="synthetic-independent-refresh-reviewer",
        reviewed_at=(NOW + timedelta(minutes=1)).isoformat(),
        primary_source_check="verified",
        checked_source_ids=["annual", "price"],
        recalculated_projections=prior.review.model_dump(mode="json")["recalculated_projections"],
        strongest_countercase=raw["conclusion"]["strongest_countercase"],
        review_scope="Aの保存根拠と最新Bの補足を読み直し、今回の全coreと数値を独立検証した。",
    )
    review_path.write_text(yaml.safe_dump(review))
    promoted = promote(
        workspace=workspace, ticker="1234", db_path=db, now=NOW + timedelta(minutes=2)
    )
    assert promoted.assessment_id == result["assessment_id"]
    from baibai_engine.storage.sqlite.application.read import connect_read_only

    with closing(connect_read_only(db)) as connection, connection:
        pair = load_reviewed_assessment(connection, promoted.assessment_id)
        header = connection.execute(
            "SELECT supersedes_id,published_at FROM stock_assessment WHERE assessment_id=?",
            (promoted.assessment_id,),
        ).fetchone()
        assert pair.document.refresh.prior_assessment_ref.assessment_id == prior.assessment_id
        assert header[0] == latest.assessment_id
        assert datetime.fromisoformat(header[1]) > datetime.fromisoformat(
            "2026-10-14T16:16:00+09:00"
        )


def test_refresh_retry_preserves_identity_but_explicit_material_change_gets_new_identity(case):
    _db, _workspace, _operation = case
    prior, latest = _prior_and_latest(case)
    first = scaffold(case, from_assessment_id=prior.assessment_id)
    same = scaffold(
        case, now=NOW + timedelta(minutes=1), force=True, from_assessment_id=prior.assessment_id
    )
    assert same["assessment_id"] == first["assessment_id"]
    switched = scaffold(case, force=True, from_assessment_id=latest.assessment_id)
    assert switched["assessment_id"] != first["assessment_id"]
    raw = yaml.safe_load(Path(switched["assessment_draft"]).read_text())
    assert raw["refresh"]["prior_assessment_ref"]["assessment_id"] == latest.assessment_id
    identity = yaml.safe_load(
        (Path(switched["assessment_draft"]).parent / "assessment-publication.yaml").read_text()
    )
    assert identity["supersedes_id"] == latest.assessment_id


def test_refresh_file_failure_retry_preserves_scaffolded_identity(case, monkeypatch):
    _db, workspace, _operation = case
    prior, latest = _prior_and_latest(case)
    writer = scaffold_module.write_text_new

    def fail_draft(path, content):
        if path.name == "assessment-draft.yaml":
            raise OSError("synthetic draft write failure")
        writer(path, content)

    monkeypatch.setattr(scaffold_module, "write_text_new", fail_draft)
    with pytest.raises(OSError, match="draft write failure"):
        scaffold(case, from_assessment_id=prior.assessment_id)
    identity = yaml.safe_load((workspace / "1234/assessment-publication.yaml").read_text())
    assert not (workspace / "1234/assessment-draft.yaml").exists()
    monkeypatch.setattr(scaffold_module, "write_text_new", writer)
    resumed = scaffold(case, from_assessment_id=prior.assessment_id)
    assert resumed["assessment_id"] == identity["assessment_id"]
    assert identity["supersedes_id"] == latest.assessment_id


@pytest.mark.parametrize("completed", [False, True])
def test_published_holding_workspace_status_remains_readable_after_sale(tmp_path, completed):
    db = tmp_path / "app"
    ledger = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    seed_ledger(db, ledger)
    value = seed_generated_selection(
        runs=tmp_path / "runs", now=NOW, tickers=("2331",), scope="review_positions"
    )
    prepared = prepare_selected_workspace(
        draft=generated_draft_for(value),
        db_path=db,
        workspace=tmp_path / "workspace",
        runs_path=tmp_path / "runs",
        now=NOW,
    )
    child = prepared.workspace / "holdings/2331"
    author((db, child, prepared.operation_id), ticker="2331")
    before = ledger.model_dump(mode="json")
    published = promote(workspace=child, ticker="2331", db_path=db, now=NOW)
    service = OperationService(db)
    operation = service.get(prepared.operation_id)
    progress = progress_binding(operation.payload)
    assert progress.outcomes[0].purpose == "holding"
    assert progress.outcomes[0].assessment_ref.assessment_id == published.assessment_id
    assert (
        compute_status(prepared.workspace, db_path=db, now=NOW)["workspace_status"] == "published"
    )
    from baibai_engine.portfolio.ledger.store import LedgerStoreService

    assert LedgerStoreService(db).load().model_dump(mode="json") == before
    if completed:
        from tests.helpers.operation_proposal import (
            proposal_for,
            seed_proposal,
        )

        proposal = proposal_for(operation, actions={"2331": "hold"}).model_copy(
            update={"published_at": NOW + timedelta(seconds=1)}
        )
        seed_proposal(db, proposal)
        service.complete(
            prepared.operation_id,
            proposal_id=proposal.proposal_id,
            result="保有評価と提案を保存した",
            next="人間の採否を待つ",
            completed_at=NOW + timedelta(seconds=2),
        )

    from baibai_engine.portfolio.ledger.drafts import apply_draft, build_sell_execution_draft

    sold_at = NOW + timedelta(days=1)
    ledger_service = LedgerStoreService(db)
    sale = build_sell_execution_draft(
        ledger_service,
        occurred_at=sold_at,
        ticker="2331",
        quantity=200,
        price_yen=Decimal("1100"),
    )
    assert sale is not None
    apply_draft(ledger_service, sale, human_confirmed=True)
    saved_files = {path: path.read_bytes() for path in prepared.workspace.rglob("*.yaml")}
    saved_database = db.read_bytes()

    parent = compute_status(prepared.workspace, db_path=db, now=sold_at)
    child_status = compute_status(child, db_path=db, now=sold_at)

    assert parent == child_status
    clean = tmp_path / "restored-holdings"
    resume_selected_workspace(
        operation_id=prepared.operation_id, workspace=clean, db_path=db, now=sold_at
    )
    restored = compute_status(clean, db_path=db, now=sold_at)
    assert restored == compute_status(clean / "holdings/2331", db_path=db, now=sold_at)
    assert restored["workspace_status"] == "published"
    assert restored["next_action"] is None
    assert restored["cases"][0]["assessment_id"] == published.assessment_id
    assert restored["cases"][0]["recorded_outcome"]["purpose"] == "holding"
    assert parent["workspace_status"] == "published"
    assert parent["cases"][0]["assessment_id"] == published.assessment_id
    assert db.read_bytes() == saved_database
    assert {path: path.read_bytes() for path in saved_files} == saved_files
    for write in (
        lambda: scaffold_assessment(
            workspace=child,
            ticker="2331",
            sqlite_path=tmp_path / "absent",
            as_of=sold_at.date(),
            retrieved_at=sold_at,
            db_path=db,
            force=True,
        ),
        lambda: scaffold_review(workspace=child, ticker="2331", db_path=db, force=True),
        lambda: promote(workspace=child, ticker="2331", db_path=db, now=sold_at),
    ):
        with pytest.raises(
            ResearchWorkspaceConflictError,
            match="completed research Operation" if completed else "current holding",
        ):
            write()
    assert db.read_bytes() == saved_database
    assert {path: path.read_bytes() for path in saved_files} == saved_files


def test_public_cli_uses_scaffolded_identity(case, capsys):
    db, workspace, _ = case
    assert (
        main(
            [
                "stock-assessment-scaffold",
                "--workspace",
                str(workspace),
                "--db",
                str(db),
                "--ticker",
                "1234",
                "--asof",
                NOW.date().isoformat(),
                "--out",
                str(workspace / "1234/assessment-draft.yaml"),
            ],
            now=NOW,
        )
        == 0
    )
    identity = yaml.safe_load(capsys.readouterr().out)["assessment_id"]
    author(case)
    assert (
        main(
            [
                "stock-assessment-promote",
                "--workspace",
                str(workspace),
                "--db",
                str(db),
                "--ticker",
                "1234",
            ],
            now=NOW,
        )
        == 0
    )
    assert yaml.safe_load(capsys.readouterr().out)["assessment_id"] == identity


@pytest.mark.parametrize("entry", ["promotion", "reuse_scaffold"])
@pytest.mark.parametrize("key", ["assessment_id", "supersedes_id"])
def test_workspace_refuses_ambiguous_assessment_publication_identity(case, capsys, entry, key):
    db, workspace, _ = case
    if key == "supersedes_id":
        prior, latest = _prior_and_latest(case)
    formed = scaffold(case)
    author(case)
    identity = workspace / "1234/assessment-publication.yaml"
    original = identity.read_text(encoding="utf-8")
    if key == "assessment_id":
        assert formed["assessment_id"] != assessment_id(81)
        identity.write_text(original + f"assessment_id: {assessment_id(81)}\n", encoding="utf-8")
    else:
        assert yaml.safe_load(original)["supersedes_id"] == latest.assessment_id
        identity.write_text(f"supersedes_id: {prior.assessment_id}\n" + original, encoding="utf-8")
    before = db.read_bytes()
    files = {path: path.read_bytes() for path in workspace.rglob("*.yaml")}
    export = workspace.parent / "separate.yaml"
    args = (
        _promote_command(db, workspace)
        if entry == "promotion"
        else [
            "stock-assessment-scaffold",
            "--workspace",
            str(workspace),
            "--db",
            str(db),
            "--ticker",
            "1234",
            "--sqlite-path",
            str(workspace / "absent"),
            "--asof",
            NOW.date().isoformat(),
            "--force",
            "--out",
            str(export),
        ]
    )
    assert main(args, now=NOW) == 3
    result = capsys.readouterr()
    assert result.out == ""
    assert result.err == "error: invalid assessment publication YAML\n"
    assert db.read_bytes() == before
    assert {path: path.read_bytes() for path in workspace.rglob("*.yaml")} == files
    assert not export.exists()


def test_review_scaffold_rejects_an_incomplete_assessment_without_writing_review(case):
    db, workspace, _ = case
    scaffold(case)
    with pytest.raises(AssessmentError):
        scaffold_review(workspace=workspace, ticker="1234", db_path=db)
    assert not (workspace / "1234/assessment-review.yaml").exists()


def test_review_scaffold_cli_keeps_invalid_source_output(case, capsys):
    db, workspace, _ = case
    scaffold(case)
    assert (
        main(
            [
                "independent-review-scaffold",
                "--workspace",
                str(workspace),
                "--db",
                str(db),
                "--ticker",
                "1234",
            ],
            now=NOW,
        )
        == 3
    )
    output = capsys.readouterr()
    assert output.err == ""
    payload = json.loads(output.out)
    assert payload["status"] == "invalid_source"
    assert "validation errors for StockAssessmentDocument" in payload["reason"]
    assert not (workspace / "1234/assessment-review.yaml").exists()


def save_research_seed(case, *, ticker="1234", recorded_at=NOW, questions_only=False):
    from baibai_engine.investment_judgment.company_research.document import ResearchDraft
    from baibai_engine.investment_judgment.company_research.store import ResearchStoreService

    document = {
        "ticker": ticker,
        "as_of": NOW.date().isoformat(),
        "investigated_scope": "受注と顧客依存を確認",
        "unresolved_questions": ["更新契約の採算は維持されるか"],
    }
    if not questions_only:
        document.update(
            sources=[
                {
                    "source_id": "market_close",
                    "ticker": ticker,
                    "source_tier": "primary",
                    "ref": "https://example.test/ir",
                    "retrieved_at": NOW.isoformat(),
                    "as_of": "2026-10-13",
                    "used_for": "受注",
                }
            ],
            facts=[
                {
                    "fact_id": "market_price_close",
                    "fact_kind": "other",
                    "value": 42,
                    "unit": "contracts",
                    "as_of": "2026-10-13",
                    "source_ids": ["market_close"],
                }
            ],
            business_understanding={"text": "継続契約が中心", "source_ids": ["market_close"]},
            value_capture_hypothesis={"text": "採算改善の可能性", "source_ids": ["market_close"]},
            hypotheses=[
                {
                    "statement": "顧客依存が小さい",
                    "origin": "research",
                    "result": "refuted",
                    "reason": "大口顧客へ集中",
                    "source_ids": ["market_close"],
                }
            ],
        )
    return ResearchStoreService(case[0], clock=lambda: recorded_at).save(
        ResearchDraft.model_validate({"research_id": "seed-r1", "document": document})
    )


def test_research_seed_preserves_evidence_without_authoring_value_or_review(case):
    from tests.helpers.screening_sqlite import seed_daily_bars

    from baibai_engine.investment_judgment.company_research.store import load_research
    from baibai_engine.storage.sqlite.application.read import connect_read_only

    record = save_research_seed(case)
    db, workspace, _ = case
    market = workspace / "market.sqlite"
    seed_daily_bars(market, [("1234", NOW.date().isoformat(), 100.0, 1.0)])
    result = scaffold_assessment(
        workspace=workspace,
        ticker="1234",
        sqlite_path=market,
        as_of=NOW.date(),
        retrieved_at=NOW,
        db_path=db,
        from_research_id="seed-r1",
    )
    text = Path(result["assessment_draft"]).read_text()
    raw = yaml.safe_load(text)
    assert record.reference.payload_sha256 in text
    assert result["from_research_id"] == "seed-r1"
    assert [s["source_id"] for s in raw["input_snapshot"]["sources"]] == [
        "research_market_close",
        "market_close",
    ]
    fact = raw["input_snapshot"]["facts"][0]
    assert fact["fact_id"] == "research_market_price_close"
    assert fact["source_ids"] == ["research_market_close"]
    assert fact["as_of"] == "2026-10-13"
    assert raw["valuation"]["market_price_fact_id"] == "market_price_close"
    assert raw["valuation"]["status"] == "unresolved"
    assert raw["valuation"]["base"] is None
    assert raw["conclusion"]["stance"] == "uncertain"
    assert raw["findings"]["value_gap_explanation"] is None
    assert raw["investment_case"] == {
        "explanation": "未採択のResearch価値仮説（今回の評価で検証）:\n採算改善の可能性",
        "source_ids": ["research_market_close"],
        "invalidation_conditions": [],
        "status": "uncertain",
        "status_reason": None,
    }
    assert raw["findings"]["business_understanding"] == "継続契約が中心"
    assert raw["findings"]["hypotheses"][0]["source_ids"] == ["research_market_close"]
    assert raw["findings"]["unresolved_questions"] == ["更新契約の採算は維持されるか"]
    assert not (workspace / "1234/assessment-review.yaml").exists()
    with closing(connect_read_only(db)) as connection:
        assert load_research(connection, "seed-r1") == record


@pytest.mark.parametrize("value_only", [False, True])
def test_research_value_capture_material_survives_with_its_own_sources(case, value_only):
    from copy import deepcopy

    from baibai_engine.investment_judgment.company_research.document import ResearchDraft
    from baibai_engine.investment_judgment.company_research.store import (
        ResearchStoreService,
        load_research,
    )
    from baibai_engine.storage.sqlite.application.read import connect_read_only

    original = save_research_seed(case)
    document = original.document.model_dump(mode="json")
    separate = deepcopy(document["sources"][0])
    separate.update(source_id="industry", used_for="費用変化から普通株cashへの仮説")
    document["sources"].append(separate)
    # A later counterargument and multiline text must remain material, not parsed
    # instructions or an automatically adopted investment conclusion.
    text = (
        "投入費用の低下が更新契約のcashへ届くか。" * 30
        + "\nただし価格転嫁で還元されるなら採算改善は反証される。"
    )
    document["value_capture_hypothesis"] = {"text": text, "source_ids": ["industry"]}
    if value_only:
        document.update(
            business_understanding=None, facts=[], hypotheses=[], unresolved_questions=[]
        )
    record = ResearchStoreService(case[0], clock=lambda: NOW).save(
        ResearchDraft.model_validate(
            {
                "research_id": "seed-r2",
                "prior_research_ref": original.reference.model_dump(mode="json"),
                "document": document,
            }
        )
    )
    before = case[0].read_bytes()
    result = scaffold(case, from_research_id="seed-r2")
    raw = yaml.safe_load(Path(result["assessment_draft"]).read_text())
    assert raw["investment_case"]["explanation"] == (
        "未採択のResearch価値仮説（今回の評価で検証）:\n" + text
    )
    assert raw["investment_case"]["source_ids"] == ["research_industry"]
    assert raw["investment_case"]["status"] == "uncertain"
    assert raw["investment_case"]["status_reason"] is None
    assert raw["investment_case"]["invalidation_conditions"] == []
    assert raw["valuation"]["status"] == "unresolved"
    assert raw["valuation"]["base"] is None
    assert raw["findings"]["value_gap_explanation"] is None
    assert raw["findings"]["hypotheses"] == [
        {**item, "source_ids": ["research_" + ref for ref in item["source_ids"]]}
        for item in document["hypotheses"]
    ]
    with pytest.raises(ValidationError):
        StockAssessmentDocument.model_validate(raw)
    with closing(connect_read_only(case[0])) as connection:
        assert load_research(connection, "seed-r1") == original
        assert load_research(connection, "seed-r2") == record
    assert case[0].read_bytes() == before
    assert not (case[1] / "1234/assessment-review.yaml").exists()


def test_question_only_research_seed_is_allowed(case):
    save_research_seed(case, questions_only=True)
    result = scaffold(case, from_research_id="seed-r1")
    raw = yaml.safe_load(Path(result["assessment_draft"]).read_text())
    assert raw["input_snapshot"]["sources"] == []
    assert raw["findings"]["business_understanding"] is None
    assert raw["investment_case"]["explanation"] is None
    assert raw["investment_case"]["source_ids"] == []
    assert raw["findings"]["unresolved_questions"] == ["更新契約の採算は維持されるか"]


@pytest.mark.parametrize("questions_only", [False, True])
@pytest.mark.parametrize("text_form", ["multiline", "controls", "unicode_newlines"])
def test_research_scope_is_visible_without_becoming_current_assessment(
    case, questions_only, text_form
):
    from baibai_engine.investment_judgment.company_research.document import ResearchDraft
    from baibai_engine.investment_judgment.company_research.store import (
        ResearchStoreService,
        load_research,
    )
    from baibai_engine.storage.sqlite.application.read import connect_read_only

    original = save_research_seed(case, questions_only=questions_only)
    document = original.document.model_dump(mode="json")
    # Only the original scope carries these period/industry limits. Content that
    # resembles YAML must remain commentary, not rewrite the author's payload.
    scope = (
        "2026年4–6月の国内部品群だけ。世界動向の企業への転嫁は未確認。\n"
        + "原典検索範囲。" * 60
        + "\n2027年通期・海外事業を調べた記録ではない。\n"
        "---\nconclusion:\n  stance: supported\n..."
    )
    if text_form == "controls":
        scope += "\n範囲未確認\r\n引用: '本文'\t\x00\x7f\x80\x85\u2028対象期不明\u2029末尾"
    elif text_form == "unicode_newlines":
        scope = "2026年4–6月の国内だけ。\x85次期は未確認。\u2028海外は対象外。\u2029末尾"
    document["investigated_scope"] = scope
    draft = ResearchDraft.model_validate(
        {
            "research_id": "scope-r2",
            "prior_research_ref": original.reference.model_dump(mode="json"),
            "document": document,
        }
    )
    service = ResearchStoreService(case[0], clock=lambda: NOW + timedelta(seconds=1))
    record = service.save(draft)
    later = NOW + timedelta(days=1)
    first = scaffold(case, now=later, from_research_id="seed-r1")
    original_payload = yaml.safe_load(Path(first["assessment_draft"]).read_text())
    before = case[0].read_bytes()
    exported = case[1] / "scope-export.yaml"
    result = scaffold(case, now=later, from_research_id="scope-r2", force=True, out=exported)
    text = Path(result["assessment_draft"]).read_text()
    raw = yaml.safe_load(text)

    header = f"# 元Researchの調査範囲（{record.document.as_of} 時点・未採択の補助）:\n"
    disclaimer = "# 当時の調査範囲であり、今回の評価範囲・再確認済みを意味しない。\n"
    assert header in text
    assert disclaimer in text
    comment = text.split(header, 1)[1].split(disclaimer, 1)[0]
    assert all(line.startswith("# ") for line in comment.splitlines())
    restored_scope = yaml.safe_load("\n".join(line[2:] for line in comment.splitlines()))
    assert restored_scope == {"investigated_scope": scope}
    assert record.reference.research_id in text
    assert record.reference.payload_sha256 in text
    assert exported.read_text() == text
    assert raw == original_payload  # Scope commentary cannot author a current conclusion.
    assert raw["valuation"]["status"] == "unresolved"
    assert raw["conclusion"]["stance"] == "uncertain"
    with pytest.raises(ValidationError):
        StockAssessmentDocument.model_validate(raw)
    with closing(connect_read_only(case[0])) as connection:
        assert load_research(connection, original.reference.research_id) == original
        assert load_research(connection, record.reference.research_id) == record
    assert case[0].read_bytes() == before
    assert service.save(draft) == record
    assert not (case[1] / "1234/assessment-review.yaml").exists()


@pytest.mark.parametrize("scope", ["", " \t\r\n"])
def test_empty_research_scope_remains_invalid_before_assessment_seeding(scope):
    from baibai_engine.investment_judgment.company_research.document import ResearchDocument

    with pytest.raises(ValidationError, match="investigated_scope"):
        ResearchDocument(
            ticker="1234",
            as_of=NOW.date(),
            investigated_scope=scope,
            unresolved_questions=("対象期は未確認",),
        )


@pytest.mark.parametrize("invalid", ["ticker", "future", "both", "unknown"])
def test_research_seed_rejects_wrong_identity_or_time_before_writing(case, invalid):
    from baibai_engine.investment_judgment.company_research.store import ResearchReadError

    save_research_seed(
        case,
        ticker="5678" if invalid == "ticker" else "1234",
        recorded_at=NOW + timedelta(seconds=1) if invalid == "future" else NOW,
    )
    with pytest.raises(
        (ResearchWorkspaceDataError, ResearchWorkspaceConflictError, ResearchReadError)
    ):
        scaffold(
            case,
            from_research_id="missing" if invalid == "unknown" else "seed-r1",
            from_assessment_id="unused" if invalid == "both" else None,
        )
    assert not (case[1] / "1234/assessment-draft.yaml").exists()


def test_promote_releases_validated_selection_before_binding_publication(case, monkeypatch):
    import gc
    import weakref

    from baibai_engine.investment_judgment.workflow.workspace import publication

    db, workspace, _ = case
    author(case)
    original = publication.require_authoring_selection
    record = OperationService.record_assessment
    references = []

    def observe(*args, **kwargs):
        selection = original(*args, **kwargs)
        references.append(weakref.ref(selection))
        return selection

    def bind_after_release(self, *args, **kwargs):
        gc.collect()
        assert len(references) == 2  # Early workspace check and locked App write admission.
        assert all(reference() is None for reference in references)
        return record(self, *args, **kwargs)

    monkeypatch.setattr(publication, "require_authoring_selection", observe)
    monkeypatch.setattr(OperationService, "record_assessment", bind_after_release)
    result = promote(workspace=workspace, ticker="1234", db_path=db, now=NOW)
    assert result.assessment_id == assessment_id()


def test_status_reuses_the_validated_workspace_selection_once(case, monkeypatch):
    from baibai_engine.investment_judgment.workflow.workspace import status

    db, workspace, _ = case
    original = status.validate_workspace_selection
    calls = []

    def observe(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(status, "validate_workspace_selection", observe)
    before = db.read_bytes()
    result = compute_status(workspace, db_path=db, now=NOW)
    assert result["workspace_status"] == "incomplete"
    assert len(calls) == 1
    assert db.read_bytes() == before


@pytest.mark.parametrize("source", ["app", "market"])
@pytest.mark.parametrize("alias", ["direct", "relative", "symlink", "hardlink"])
def test_forced_scaffold_export_cannot_replace_input_database(
    case, tmp_path, monkeypatch, capsys, source, alias
):
    from baibai_engine.storage.sqlite.source_cache.schema import open_connection

    db, workspace, _ = case
    market = tmp_path / "market.sqlite"
    open_connection(market).close()
    target = db if source == "app" else market
    output = target
    if alias == "relative":
        monkeypatch.chdir(tmp_path)
        output = Path(target.name)
    elif alias != "direct":
        output = tmp_path / "alias.sqlite"
        if alias == "symlink":
            output.symlink_to(target)
        else:
            output.hardlink_to(target)
    before = db.read_bytes(), market.read_bytes(), output.read_bytes()
    saved_files = {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()}

    assert (
        main(
            [
                "stock-assessment-scaffold",
                "--workspace",
                str(workspace),
                "--db",
                str(db),
                "--ticker",
                "1234",
                "--asof",
                NOW.date().isoformat(),
                "--sqlite-path",
                str(market),
                "--out",
                str(output),
                "--force",
            ],
            now=NOW,
        )
        == 4
    )

    assert "assessment export must not replace an input database" in capsys.readouterr().err
    assert (db.read_bytes(), market.read_bytes(), output.read_bytes()) == before
    assert output.is_symlink() == (alias == "symlink")
    assert {
        path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()
    } == saved_files


@pytest.mark.parametrize("path_source", ["environment", "default"])
def test_scaffold_export_protects_resolved_application_path(case, monkeypatch, path_source):
    from baibai_engine.storage.sqlite.application import paths

    db, workspace, _ = case
    if path_source == "environment":
        monkeypatch.setenv("BAIBAI_DB", str(db))
    else:
        monkeypatch.delenv("BAIBAI_DB", raising=False)
        monkeypatch.setattr(paths, "DEFAULT_DB_PATH", db)
    before = db.read_bytes()
    with pytest.raises(ResearchWorkspaceConflictError, match="input database"):
        scaffold_assessment(
            workspace=workspace,
            ticker="1234",
            sqlite_path=workspace / "absent",
            as_of=NOW.date(),
            retrieved_at=NOW,
            out=db,
            force=True,
        )
    assert db.read_bytes() == before
    assert not (workspace / "1234").exists()


def test_scaffold_export_cannot_create_yaml_at_an_absent_market_input(case):
    _db, workspace, _ = case
    market = workspace / "absent"
    with pytest.raises(ResearchWorkspaceConflictError, match="input database"):
        scaffold(case, out=market, force=True)
    assert not market.exists()
    assert not (workspace / "1234").exists()


def test_scaffold_force_still_replaces_a_separate_export(case):
    db, workspace, _ = case
    output = workspace.parent / "separate.yaml"
    first = scaffold(case, out=output)
    before = db.read_bytes()
    output.write_text("synthetic previous export")
    second = scaffold(case, out=output, force=True)
    assert output.read_bytes() == (workspace / "1234/assessment-draft.yaml").read_bytes()
    assert second["assessment_id"] == first["assessment_id"]
    assert db.read_bytes() == before


@pytest.fixture(params=["new", "holding"])
def workspace_export_case(case, tmp_path, request):
    if request.param == "new":
        db, workspace, _ = case
        return db, workspace, workspace, "1234"
    root = tmp_path / "holding"
    root.mkdir()
    db = root / "app"
    seed_ledger(
        db, load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    )
    value = seed_generated_selection(
        runs=root / "runs", now=NOW, tickers=("2331",), scope="review_positions"
    )
    prepared = prepare_selected_workspace(
        draft=generated_draft_for(value),
        db_path=db,
        workspace=root / "workspace",
        runs_path=root / "runs",
        now=NOW,
    )
    return db, prepared.workspace / "holdings/2331", prepared.workspace, "2331"


@pytest.mark.parametrize("source", ["manifest", "domain_inputs"])
@pytest.mark.parametrize("alias", ["direct", "relative", "symlink", "hardlink"])
def test_forced_scaffold_export_cannot_replace_fixed_workspace_input(
    workspace_export_case, tmp_path, monkeypatch, capsys, source, alias
):
    db, workspace, root, ticker = workspace_export_case
    target = workspace / "manifest.yaml" if source == "manifest" else root / "domain-inputs.yaml"
    output = target
    if alias == "relative":
        monkeypatch.chdir(tmp_path)
        output = target.relative_to(tmp_path)
    elif alias != "direct":
        output = tmp_path / "input-alias.yaml"
        if alias == "symlink":
            output.symlink_to(target)
        else:
            output.hardlink_to(target)
    runs = root.parent / "runs"
    before_db = db.read_bytes(), runs.read_bytes()
    before_output = output.read_bytes()
    before_files = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

    assert (
        main(
            [
                "stock-assessment-scaffold",
                "--workspace",
                str(workspace),
                "--db",
                str(db),
                "--ticker",
                ticker,
                "--sqlite-path",
                str(root / "absent"),
                "--asof",
                NOW.date().isoformat(),
                "--out",
                str(output),
                "--force",
            ],
            now=NOW,
        )
        == 4
    )

    assert "assessment export must not replace a fixed workspace input" in capsys.readouterr().err
    assert (db.read_bytes(), runs.read_bytes()) == before_db
    assert output.read_bytes() == before_output
    assert output.is_symlink() == (alias == "symlink")
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before_files


@pytest.mark.parametrize("alias", ["direct", "relative", "symlink"])
def test_scaffold_export_keeps_its_canonical_draft_and_force(
    workspace_export_case, tmp_path, monkeypatch, alias
):
    db, workspace, root, ticker = workspace_export_case
    canonical = workspace / ticker / "assessment-draft.yaml"
    output = canonical
    if alias == "relative":
        monkeypatch.chdir(tmp_path)
        output = canonical.relative_to(tmp_path)
        workspace = workspace.relative_to(tmp_path)
    elif alias == "symlink":
        output = tmp_path / "draft-alias.yaml"
        output.symlink_to(canonical)
    before = db.read_bytes(), (root / "domain-inputs.yaml").read_bytes()
    identifiers = []
    for force in (False, True):
        result = scaffold_assessment(
            workspace=workspace,
            ticker=ticker,
            sqlite_path=root / "absent",
            as_of=NOW.date(),
            retrieved_at=NOW,
            db_path=db,
            out=output,
            force=force,
        )
        identifiers.append(result["assessment_id"])
        assert output.read_bytes() == canonical.read_bytes()
    assert identifiers[0] == identifiers[1]
    assert (db.read_bytes(), (root / "domain-inputs.yaml").read_bytes()) == before


@pytest.mark.parametrize(
    ("target", "entry"),
    [
        ("assessment", "evaluate"),
        ("assessment", "promote"),
        ("assessment", "independent-review-scaffold"),
        ("review", "evaluate"),
        ("review", "promote"),
    ],
)
def test_ambiguous_authored_pair_is_not_evaluated_or_saved(case, capsys, target, entry):
    db, workspace, _ = case
    author(case)
    pair_dir = workspace / "1234"
    document = pair_dir / "assessment-draft.yaml"
    review = pair_dir / "assessment-review.yaml"
    broken = document if target == "assessment" else review
    key = "kind" if target == "assessment" else "review_id"
    private = "private-synthetic-assessment-text"
    text = f"{key}: {private}\n" + broken.read_text(encoding="utf-8")
    broken.write_text(text, encoding="utf-8")
    before = db.read_bytes()
    files = {path: path.read_bytes() for path in workspace.rglob("*.yaml")}
    common = ["--workspace", str(workspace), "--db", str(db), "--ticker", "1234"]
    if entry == "evaluate":
        assert (
            main(["stock-assessment-evaluate", str(document), "--review", str(review)], now=NOW)
            == 2
        )
    elif entry == "promote":
        assert main(["stock-assessment-promote", *common], now=NOW) == 3
    else:
        assert main([entry, *common, "--force"], now=NOW) == 3
    result = capsys.readouterr()
    label = "assessment" if target == "assessment" else "Assessment Review"
    assert f"invalid {label} YAML" in result.err + result.out
    assert private not in result.err + result.out
    assert db.read_bytes() == before
    assert {path: path.read_bytes() for path in workspace.rglob("*.yaml")} == files
