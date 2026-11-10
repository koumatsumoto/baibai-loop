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
