"""The temporary cutover changes schema, not the user's recorded decisions."""

import json
import sqlite3
from contextlib import closing

import pytest
from tools.cutover.prepare_application_v25 import prepare

from baibai_engine.position.ledger import replay_events_through
from baibai_engine.position.ledger_read import load_ledger_document
from baibai_engine.storage.application.schema import SCHEMA_SQL


def source_store(path):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_SQL)
    connection.execute("CREATE TABLE ledger_market_price(ticker TEXT PRIMARY KEY, payload TEXT)")
    connection.execute("PRAGMA user_version = 24")
    connection.execute("INSERT INTO ledger_market_price VALUES (?, ?)", ("2915", '{"price":2020}'))
    connection.commit()
    return connection


def add_ledger(connection):
    header = {
        "schema_version": 2,
        "portfolio_scope": "repository_only",
        "as_of": "2026-10-15T15:30:00+09:00",
        "estimated_exit_tax_rate_bps": None,
        "estimated_exit_tax_basis": None,
        "overrides": [],
    }
    connection.execute(
        "INSERT INTO ledger_meta VALUES (1, 2, 'repository_only', ?, NULL, NULL, ?)",
        (header["as_of"], json.dumps(header)),
    )
    event = {
        "event_id": "opening",
        "type": "opening_balance",
        "occurred_at": "2026-10-15T00:00:00+09:00",
        "occurred_on": "2026-10-15",
        "amount_yen": 100000,
    }
    connection.execute(
        "INSERT INTO ledger_event VALUES (1, 'opening', ?, 0, 'opening_balance', NULL, NULL, ?)",
        (event["occurred_at"], json.dumps(event)),
    )
    return header


def test_keeps_wal_business_records_and_original_price_cache(tmp_path):
    source, output = tmp_path / "source.sqlite", tmp_path / "candidate.sqlite"
    with closing(source_store(source)) as original:
        original.execute("PRAGMA journal_mode = WAL")
        header = add_ledger(original)
        original.execute(
            "INSERT INTO market_context_assessment VALUES "
            "('context', 1, '2026-10-15', '2026-10-15T00:00:00Z', NULL, '{}')"
        )
        original.execute("INSERT INTO market_context_assessment_head VALUES (1, 'context')")
        original.commit()
        prepare(source, output)
        assert original.execute("PRAGMA user_version").fetchone() == (24,)
        assert original.execute("SELECT COUNT(*) FROM ledger_market_price").fetchone() == (1,)
        with closing(sqlite3.connect(output)) as candidate:
            assert candidate.execute("PRAGMA user_version").fetchone() == (25,)
            tables = original.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' "
                "AND name NOT IN ('ledger_market_price', 'ledger_meta')"
            ).fetchall()
            for (table,) in tables:
                assert (
                    candidate.execute(f'SELECT * FROM "{table}"').fetchall()
                    == original.execute(f'SELECT * FROM "{table}"').fetchall()
                )
            before = original.execute("SELECT * FROM ledger_meta").fetchone()
            after = candidate.execute("SELECT * FROM ledger_meta").fetchone()
            assert before[1] == 2
            assert after[1] == 3
            assert after[:1] + after[2:-1] == before[:1] + before[2:-1]
            assert json.loads(before[-1]) == header
            assert json.loads(after[-1]) == {**header, "schema_version": 3}
            document = load_ledger_document(candidate)
            assert document is not None
            assert document.events[0].occurred_on.isoformat() == "2026-10-15"
            state = replay_events_through(document.events, document.as_of)
            assert state.available_cash_yen == 100000
            assert not candidate.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'ledger_market_price'"
            ).fetchall()


@pytest.mark.parametrize("fault", ["wrong_version", "missing_table", "broken_reference"])
def test_refuses_wrong_or_broken_source_without_retaining_a_candidate(tmp_path, fault):
    source, output = tmp_path / "source.sqlite", tmp_path / "candidate.sqlite"
    with closing(source_store(source)) as original:
        if fault == "wrong_version":
            original.execute("PRAGMA user_version = 23")
        elif fault == "missing_table":
            original.execute("DROP TABLE task")
        else:
            original.execute("INSERT INTO market_context_assessment_head VALUES (1, 'missing')")
        original.commit()
    original_bytes = source.read_bytes()
    with pytest.raises(ValueError, match=r"expected|shape|references"):
        prepare(source, output)
    assert not output.exists()
    assert source.read_bytes() == original_bytes


@pytest.mark.parametrize("same_path", [False, True])
def test_never_overwrites_a_source_or_existing_candidate(tmp_path, same_path):
    source, output = tmp_path / "source.sqlite", tmp_path / "candidate.sqlite"
    source_store(source).close()
    if same_path:
        output = source
    else:
        output.write_bytes(b"existing candidate")
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        prepare(source, output)
    assert output.read_bytes() == before


def test_empty_unimported_ledger_remains_absent(tmp_path):
    source, output = tmp_path / "source.sqlite", tmp_path / "candidate.sqlite"
    source_store(source).close()
    prepare(source, output)
    with closing(sqlite3.connect(output)) as candidate:
        assert load_ledger_document(candidate) is None


@pytest.mark.parametrize("fault", ["column", "payload", "non_object", "orphan", "bad_event"])
def test_refuses_unexpected_ledger_without_modifying_source(tmp_path, fault):
    source, output = tmp_path / "source.sqlite", tmp_path / "candidate.sqlite"
    with closing(source_store(source)) as original:
        add_ledger(original)
        if fault == "column":
            original.execute("UPDATE ledger_meta SET schema_version = 3")
        elif fault == "payload":
            original.execute(
                "UPDATE ledger_meta SET payload = json_set(payload, '$.schema_version', 3)"
            )
        elif fault == "non_object":
            original.execute("UPDATE ledger_meta SET payload = '[]'")
        elif fault == "orphan":
            original.execute("DELETE FROM ledger_meta")
        else:
            original.execute("UPDATE ledger_event SET payload = '{}'")
        original.commit()
    before = source.read_bytes()
    with pytest.raises(ValueError, match=r"expected a ledger|without metadata|validation error"):
        prepare(source, output)
    assert not output.exists()
    assert source.read_bytes() == before
