"""The explicit cutover copies schema 29, never rewrites saved financial evidence."""

import sqlite3

import pytest
from tests.engine.portfolio.ledger.test_share_conversions import _apply, _ledger
from tests.helpers.outcome_publication import _publication
from tools.enable_share_conversions import copy_with_share_conversions

from baibai_engine.portfolio.ledger.store import LedgerStoreService, _event_values, _meta_values
from baibai_engine.portfolio.performance.store import PortfolioOutcomeStore
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.schema import SCHEMA_SQL


def _old_store(path):
    document = _ledger()
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA_SQL.replace(", 'share_conversion'", ""))
        c.execute("PRAGMA user_version=29")
        c.execute("INSERT INTO ledger_meta VALUES(?,?,?,?,?,?,?)", _meta_values(document))
        for i, event in enumerate(document.events, 1):
            c.execute(
                "INSERT INTO ledger_event VALUES(?,?,?,?,?,?,?,?)", _event_values(i, event, 0)
            )
        publication = _publication()
        c.execute(
            "INSERT INTO portfolio_outcome VALUES(?,?,?,?,?,?)",
            (
                publication.outcome_id,
                publication.horizon,
                publication.period_start_date,
                publication.period_end_date,
                publication.status,
                canonical_json(publication.payload),
            ),
        )
        # Include opaque retired data. The copy must neither interpret nor drop it.
        c.execute("CREATE TABLE task(task_id TEXT PRIMARY KEY,payload TEXT)")
        c.execute("INSERT INTO task VALUES(?,?)", ("retired", '{"private":"synthetic"}'))
    return document


def _rows(path):
    with sqlite3.connect(path) as c:
        return {
            name: c.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            for (name,) in c.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }


def test_cutover_and_new_event_preserve_old_payload_bytes_and_outcome_retry(tmp_path):
    old, new = tmp_path / "old.sqlite", tmp_path / "new.sqlite"
    document = _old_store(old)
    before = old.read_bytes()
    rows = _rows(old)
    copy_with_share_conversions(old, new)
    assert old.read_bytes() == before
    assert _rows(new) == rows
    assert LedgerStoreService(new).load() == document
    assert not PortfolioOutcomeStore(new).publish(_publication())
    outcome_before = PortfolioOutcomeStore(new).list()
    _apply(LedgerStoreService(new))
    assert PortfolioOutcomeStore(new).list() == outcome_before
    after = _rows(new)
    for table, values in rows.items():
        if table not in {"ledger_event", "ledger_meta"}:
            assert after[table] == values
    assert after["ledger_event"][: len(rows["ledger_event"])] == rows["ledger_event"]
    assert old.read_bytes() == before
    with sqlite3.connect(new) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 30
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not c.execute("PRAGMA foreign_key_check").fetchall()


def test_cutover_refuses_existing_destination_same_path_and_wrong_version(tmp_path):
    old, new = tmp_path / "old.sqlite", tmp_path / "new.sqlite"
    _old_store(old)
    before = old.read_bytes()
    with pytest.raises(ValueError, match="differ"):
        copy_with_share_conversions(old, old)
    new.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        copy_with_share_conversions(old, new)
    assert new.read_bytes() == b"existing"
    assert old.read_bytes() == before
    with sqlite3.connect(old) as c:
        c.execute("PRAGMA user_version=28")
    with pytest.raises(ValueError, match="schema 29"):
        copy_with_share_conversions(old, tmp_path / "other.sqlite")
    assert not (tmp_path / "other.sqlite").exists()


def test_saved_proposal_hash_and_fixed_context_survive_cutover_and_conversion(tmp_path):
    from datetime import date, datetime

    from tests.helpers.proposal import (
        PUBLISHED,
        backend_case,
        read_saved_proposal,
        reviewed_proposal,
    )

    from baibai_engine.investment_judgment.trade_proposal.contracts import proposal_draft_sha256
    from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService

    case = backend_case(tmp_path / "fixture")
    saved = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED
    ).publish(reviewed_proposal(case))
    fixed_hash = proposal_draft_sha256(saved)
    fixed_context = saved.model_dump(mode="json")
    # Construct an actual pre-extension schema with all saved rows, not just a version label.
    old = tmp_path / "schema29.sqlite"
    with sqlite3.connect(case.app) as source, sqlite3.connect(old) as target:
        target.executescript(SCHEMA_SQL.replace(", 'share_conversion'", ""))
        for (name,) in source.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ):
            rows = source.execute(f'SELECT * FROM "{name}"').fetchall()
            if rows:
                target.executemany(
                    f'INSERT INTO "{name}" VALUES ({",".join("?" for _ in rows[0])})', rows
                )
        target.execute("PRAGMA user_version=29")
    old_bytes = old.read_bytes()
    readback29 = read_saved_proposal(proposal_id=saved.proposal_id, db_path=old)
    assert proposal_draft_sha256(readback29) == fixed_hash
    assert readback29.model_dump(mode="json") == fixed_context
    retried = TradeProposalService(old, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        saved
    )
    assert retried == readback29
    assert old.read_bytes() == old_bytes
    upgraded = tmp_path / "schema30.sqlite"
    copy_with_share_conversions(old, upgraded)
    readback = read_saved_proposal(proposal_id=saved.proposal_id, db_path=upgraded)
    assert proposal_draft_sha256(readback) == fixed_hash
    assert readback.model_dump(mode="json") == fixed_context
    # This fixture has cash only. Confirm a synthetic direct fill, then a separate action.
    from baibai_engine.portfolio.ledger.events import ExecutionEvent, PortfolioLedgerDocument

    service = LedgerStoreService(upgraded)
    document, head = service.load_with_head()
    buy = ExecutionEvent(
        type="execution",
        event_id="synthetic-buy",
        execution_id="synthetic-buy",
        occurred_at="2026-10-15T10:00:00+09:00",
        ticker="1234",
        side="buy",
        quantity=100,
        price_yen=1000,
        sector="synthetic",
    )
    raw = document.model_dump(mode="json")
    raw["events"].append(buy.model_dump(mode="json"))
    raw["as_of"] = "2026-10-16T18:00:00+09:00"
    service.apply_document(
        expected_head=head,
        expected_document=document,
        replacement=PortfolioLedgerDocument.model_validate(raw),
    )
    _apply(
        service,
        basis_date=date(2026, 10, 16),
        confirmed_at=datetime.fromisoformat("2026-10-16T18:00:00+09:00"),
    )
    assert (
        read_saved_proposal(proposal_id=saved.proposal_id, db_path=upgraded).model_dump(mode="json")
        == fixed_context
    )
    assert (
        proposal_draft_sha256(read_saved_proposal(proposal_id=saved.proposal_id, db_path=upgraded))
        == fixed_hash
    )


def _cash_draft(service, identity="cash-added"):
    from tests.engine.portfolio.ledger.test_share_conversions import NOW

    from baibai_engine.portfolio.ledger.drafts import build_event_draft
    from baibai_engine.portfolio.ledger.events import ContributionEvent

    return build_event_draft(
        service,
        ContributionEvent(type="contribution", event_id=identity, occurred_at=NOW, amount_yen=1000),
    )


@pytest.mark.parametrize("version", [29, 30])
def test_expand_keeps_normal_writes_readonly_views_and_stale_draft_guard(tmp_path, version):
    from contextlib import closing
    from dataclasses import replace

    from baibai_engine.portfolio.ledger.drafts import apply_draft
    from baibai_engine.portfolio.ledger.read import LedgerConflictError
    from baibai_engine.storage.paths import APPLICATION_DB_PATH
    from baibai_engine.storage.sqlite.application.read import connect_read_only
    from baibai_engine.storage.sqlite.application.write import initialize_database
    from baibai_web.materialize import export_read_models

    old = tmp_path / "kept29.sqlite"
    _old_store(old)
    path = tmp_path / APPLICATION_DB_PATH
    path.parent.mkdir(parents=True)
    if version == 30:
        copy_with_share_conversions(old, path)
    else:
        path.write_bytes(old.read_bytes())
    before = path.read_bytes()
    assert initialize_database(path) == version
    assert path.read_bytes() == before
    service = LedgerStoreService(path)
    first, stale = _cash_draft(service), _cash_draft(service, "other-writer")
    apply_draft(service, first, human_confirmed=True)
    with pytest.raises(LedgerConflictError, match="stale ledger"):
        apply_draft(service, stale, human_confirmed=True)
    assert service.load().schema_version == 3
    assert service.load().events[-1].event_id == "cash-added"
    # Ordinary saved results still write on 29 and retain exact retry semantics.
    publication = replace(_publication(), outcome_id="new-ordinary-outcome")
    assert PortfolioOutcomeStore(path).publish(publication)
    assert not PortfolioOutcomeStore(path).publish(publication)
    before_read = path.read_bytes()
    assert len(export_read_models(tmp_path, tmp_path / "views-output")) == 4
    with closing(connect_read_only(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM ledger_event")
    assert path.read_bytes() == before_read


@pytest.mark.parametrize("change", ["conversion", "metadata-only"])
def test_schema29_rejects_new_ledger_format_at_transaction_owner(tmp_path, change):
    from tests.engine.portfolio.ledger.test_share_conversions import _draft

    from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument
    from baibai_engine.portfolio.ledger.read import LedgerConflictError

    old = tmp_path / "old.sqlite"
    document = _old_store(old)
    service = LedgerStoreService(old)
    if change == "conversion":
        draft = _draft(service)
        assert draft is not None
        replacement = draft.replacement
    else:
        replacement = PortfolioLedgerDocument.model_validate(
            {**document.model_dump(mode="json"), "schema_version": 4}
        )
    before = old.read_bytes()
    # Call the owner directly: bypassing CLI/draft must not leak v4 to old readers.
    with pytest.raises(LedgerConflictError, match="schema 29 permits only metadata v3"):
        service.apply_document(
            expected_head=len(document.events),
            expected_document=document,
            replacement=replacement,
        )
    assert old.read_bytes() == before
    upgraded = tmp_path / "new.sqlite"
    copy_with_share_conversions(old, upgraded)
    LedgerStoreService(upgraded).apply_document(
        expected_head=len(document.events), expected_document=document, replacement=replacement
    )
    assert LedgerStoreService(upgraded).load() == replacement


@pytest.mark.parametrize("version", [28, 31])
def test_expand_never_accepts_other_written_versions(tmp_path, version):
    from baibai_engine.storage.sqlite.application.read import connect_read_only
    from baibai_engine.storage.sqlite.application.write import initialize_database

    path = tmp_path / "unknown.sqlite"
    _old_store(path)
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={version}")
    before = path.read_bytes()
    for open_store in (connect_read_only, initialize_database):
        with pytest.raises(RuntimeError, match=r"schema|cutover"):
            open_store(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("winner", ["migration", "ordinary-writer"])
def test_cutover_and_ordinary_writer_cannot_overwrite_each_other(tmp_path, winner):
    import subprocess
    from pathlib import Path

    from tests.helpers.r2_transfer import _environment, _fake_aws, _fake_repo

    from baibai_engine.portfolio.ledger.drafts import apply_draft

    old = tmp_path / "kept29.sqlite"
    _old_store(old)
    old_bytes = old.read_bytes()
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    env.update(
        SNAPSHOT_FAKE_COPY="1", R2_APP_BACKUP_PRUNE="skip", AWS_FAKE_EXISTING_KEY="baibai.sqlite"
    )
    remote = Path(env["AWS_FAKE_STATE"]) / "put-baibai.sqlite"
    remote.write_bytes(old_bytes)
    clients = {name: _fake_repo(tmp_path / name) for name in ("migration", "ordinary-writer")}

    def transfer(name, command):
        root = clients[name]
        return subprocess.run(
            [root / "jobs/scripts/r2_transfer.sh", command],
            cwd=root,
            env={**env, "R2_GENERATION_DIR": str(tmp_path / f"{name}-etags")},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    for name in clients:
        result = transfer(name, "pull-app")
        assert result.returncode == 0, result.stderr
    local = {name: root / "stores/application/baibai.sqlite" for name, root in clients.items()}
    # These are closed, isolated copies. Never replace a live local SQLite file.
    prepared = tmp_path / "prepared30.sqlite"
    copy_with_share_conversions(local["migration"], prepared)
    local["migration"].write_bytes(prepared.read_bytes())
    service = LedgerStoreService(local["ordinary-writer"])
    apply_draft(service, _cash_draft(service), human_confirmed=True)
    loser = next(name for name in clients if name != winner)
    unpublished = local[loser].read_bytes()
    result = transfer(winner, "push-app")
    assert result.returncode == 0, result.stderr
    published = remote.read_bytes()
    result = transfer(loser, "push-app")
    assert result.returncode != 0
    assert "baibai.sqlite changed on R2 during the merge" in result.stderr
    assert remote.read_bytes() == published
    assert local[loser].read_bytes() == unpublished
    assert old.read_bytes() == old_bytes
    assert "delete-object" not in log.read_text()
    # A fresh third client verifies the winning generation, rather than reusing local state.
    clients["readback"] = _fake_repo(tmp_path / "readback")
    result = transfer("readback", "pull-app")
    assert result.returncode == 0, result.stderr
    received = clients["readback"] / "stores/application/baibai.sqlite"
    assert _rows(received) == _rows(local[winner])
    assert (tmp_path / "readback-etags/baibai.sqlite.etag").read_bytes() == (
        tmp_path / f"{winner}-etags/baibai.sqlite.etag"
    ).read_bytes()


@pytest.mark.parametrize("version", [29, 30])
def test_expand_serializes_simultaneous_ledger_writers(tmp_path, version):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from baibai_engine.portfolio.ledger.drafts import apply_draft
    from baibai_engine.portfolio.ledger.read import LedgerConflictError

    old, upgraded = tmp_path / "old.sqlite", tmp_path / "new.sqlite"
    document = _old_store(old)
    if version == 30:
        copy_with_share_conversions(old, upgraded)
    path = old if version == 29 else upgraded
    barrier = Barrier(2)

    def writer(identity):
        service = LedgerStoreService(path)
        draft = _cash_draft(service, identity)
        barrier.wait(timeout=10)
        try:
            apply_draft(service, draft, human_confirmed=True)
        except LedgerConflictError as error:
            return str(error)
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(writer, ("first", "second"))) == ["saved", "stale ledger draft"]
    saved = LedgerStoreService(path).load()
    assert len(saved.events) == len(document.events) + 1
    assert saved.events[:-1] == document.events
    assert saved.schema_version == 3
