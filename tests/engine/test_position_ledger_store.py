from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.appdb.write import initialize_database
from baibai_engine.position.ledger import (
    ContributionEvent,
    PortfolioLedgerDocument,
    ReservationEvent,
)
from baibai_engine.position.ledger_read import LedgerConflictError
from baibai_engine.position.store import LedgerStoreService

FIXTURE = Path("tests/fixtures/portfolio-ledger/representative.yaml")


def _create_schema(path: Path) -> None:
    initialize_database(path)


def test_late_event_is_physically_appended_but_replayed_by_time(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _create_schema(path)
    source = _store_fixture()
    service = LedgerStoreService(path)
    seed_ledger(path, source)
    head = service.append_head()
    occurred_at = source.events[1].occurred_at + timedelta(seconds=1)
    event = ContributionEvent(
        event_id="human-contribution-late",
        occurred_at=occurred_at.isoformat(),
        type="contribution",
        amount_yen=100,
    )
    insertion_index = next(
        index for index, existing in enumerate(source.events) if existing.occurred_at > occurred_at
    )
    replacement = source.model_copy(
        update={
            "events": (
                *source.events[:insertion_index],
                event,
                *source.events[insertion_index:],
            )
        }
    )

    result = service.apply_document(
        expected_head=head,
        expected_document=source,
        replacement=replacement,
    )

    assert result.append_head == head + 1
    assert result.event_ids == (event.event_id,)
    assert service.load() == replacement
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT append_seq FROM ledger_event WHERE event_id = ?", (event.event_id,)
            ).fetchone()[0]
            == head + 1
        )


def test_new_same_instant_event_gets_tail_ordinal(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _create_schema(path)
    source = _store_fixture()
    service = LedgerStoreService(path)
    seed_ledger(path, source)
    same_at = source.events[2].occurred_at
    same_events = [item for item in source.events if item.occurred_at == same_at]
    event = ContributionEvent(
        event_id="human-contribution-same-instant",
        occurred_at=same_at.isoformat(),
        type="contribution",
        amount_yen=100,
    )
    insertion_index = (
        max(index for index, item in enumerate(source.events) if item.occurred_at == same_at) + 1
    )
    replacement = source.model_copy(
        update={
            "events": (*source.events[:insertion_index], event, *source.events[insertion_index:])
        }
    )

    service.apply_document(
        expected_head=service.append_head(),
        expected_document=source,
        replacement=replacement,
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT same_instant_order FROM ledger_event WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()[0] == len(same_events)


def test_stale_apply_and_event_rewrite_are_no_write(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _create_schema(path)
    source = _store_fixture()
    service = LedgerStoreService(path)
    seed_ledger(path, source)
    changed_event = source.events[1].model_copy(update={"event_id": "rewritten"})
    replacement = source.model_copy(
        update={"events": (source.events[0], changed_event, *source.events[2:])}
    )

    with pytest.raises(LedgerConflictError, match="removed"):
        service.apply_document(
            expected_head=service.append_head(),
            expected_document=source,
            replacement=replacement,
        )
    with pytest.raises(LedgerConflictError, match="stale"):
        service.apply_document(
            expected_head=service.append_head() + 1,
            expected_document=source,
            replacement=source,
        )
    assert service.load() == source


def test_decision_reference_is_indexed_without_domain_specific_foreign_key(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _create_schema(path)
    source = _store_fixture()
    service = LedgerStoreService(path)
    seed_ledger(path, source)
    template = next(item for item in source.events if isinstance(item, ReservationEvent))
    native = template.model_copy(
        update={
            "event_id": "human-open-assessment",
            "reservation_id": "reservation-assessment",
            "order_id": "order-assessment",
            "decision_reference": "capital-allocation-assessment-20260719-2331",
            "occurred_at": source.as_of,
            "expires_at": source.as_of + timedelta(days=1),
        }
    )
    replacement = source.model_copy(update={"events": (*source.events, native)})

    service.apply_document(
        expected_head=service.append_head(),
        expected_document=source,
        replacement=replacement,
    )
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT decision_reference FROM ledger_event WHERE event_id = ?",
                (native.event_id,),
            ).fetchone()[0]
            == "capital-allocation-assessment-20260719-2331"
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM ledger_event "
                "WHERE json_extract(payload, '$.decision_reference') LIKE 'https://%' "
                "AND decision_reference IS NULL"
            ).fetchone()[0]
            == 0
        )


def test_canonical_store_rejects_test_fixture_prices_without_write(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _create_schema(path)

    with pytest.raises(LedgerConflictError, match="test_fixture"):
        seed_ledger(path, load_portfolio_ledger(FIXTURE))

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM ledger_event").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM ledger_market_price").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM ledger_meta").fetchone()[0] == 0


def _store_fixture() -> PortfolioLedgerDocument:
    source = load_portfolio_ledger(FIXTURE)
    return source.model_copy(
        update={
            "market_prices": tuple(
                price.model_copy(update={"source_kind": "licensed_dataset"})
                for price in source.market_prices
            )
        }
    )


def test_ledger_queries_open_only_read_connections(tmp_path, monkeypatch):
    from baibai_engine.position import store

    path = tmp_path / "app.sqlite"
    source = _store_fixture()
    seed_ledger(path, source)
    before = path.read_bytes()
    connect = store.connect_read_only
    observed = []

    def query_only(db_path):
        connection = connect(db_path)
        observed.append(connection.execute("PRAGMA query_only").fetchone()[0])
        return connection

    def no_writer(*args, **kwargs):
        pytest.fail("ledger query opened a writer or initialized the store")

    monkeypatch.setattr(store, "connect_read_only", query_only)
    monkeypatch.setattr(store, "connect_rw", no_writer)
    monkeypatch.setattr(store, "initialize_database", no_writer)
    service = LedgerStoreService(path)
    assert service.load() == source
    assert service.append_head() == len(source.events)
    assert observed == [1, 1]
    assert path.read_bytes() == before


@pytest.mark.parametrize("unwritten", [True, False], ids=["unwritten", "initialized-unimported"])
def test_optional_reader_distinguishes_unimported_from_required_ledger(tmp_path, unwritten):
    from contextlib import closing

    from baibai_engine.appdb.read import connect_read_only
    from baibai_engine.position.ledger_read import load_ledger_document, load_ledger_in_transaction

    path = tmp_path / "app.sqlite"
    if unwritten:
        sqlite3.connect(path).close()
    else:
        initialize_database(path)
    with closing(connect_read_only(path)) as connection:
        assert load_ledger_document(connection) is None
        with pytest.raises(LedgerConflictError, match="not been imported"):
            load_ledger_in_transaction(connection)


@pytest.mark.parametrize(
    ("damage", "error"),
    [
        ("DROP TABLE ledger_meta", RuntimeError),
        ("DROP TABLE ledger_event", RuntimeError),
        ("DROP TABLE ledger_market_price", RuntimeError),
        ("ALTER TABLE ledger_meta RENAME COLUMN payload TO lost_payload", sqlite3.OperationalError),
        ("DELETE FROM ledger_meta", LedgerConflictError),
        ("UPDATE ledger_meta SET payload='[]'", ValueError),
        ("UPDATE ledger_event SET payload=json_set(payload,'$.type','invalid')", ValueError),
        (
            "UPDATE ledger_market_price SET payload=json_set(payload,'$.source_kind','test_fixture')",
            LedgerConflictError,
        ),
    ],
    ids=[
        "missing-meta-table",
        "missing-event-table",
        "missing-price-table",
        "missing-column",
        "orphaned-rows",
        "invalid-meta",
        "invalid-event",
        "fixture-price",
    ],
)
def test_shared_reader_rejects_damaged_canonical_state(tmp_path, damage, error):
    from contextlib import closing

    from baibai_engine.appdb.read import connect_read_only
    from baibai_engine.position.ledger_read import load_ledger_document

    path = tmp_path / "app.sqlite"
    seed_ledger(path, _store_fixture())
    with sqlite3.connect(path) as writer:
        writer.execute(damage)
    with closing(connect_read_only(path)) as connection, pytest.raises(error):
        load_ledger_document(connection)


def test_public_view_uses_shared_reader_validation(tmp_path):
    from baibai_engine.read_api.position import portfolio_ledger_document

    path = tmp_path / "app.sqlite"
    document = _store_fixture()
    seed_ledger(path, document)
    assert portfolio_ledger_document(path) == document
    with sqlite3.connect(path) as writer:
        writer.execute("DELETE FROM ledger_meta")
    with pytest.raises(LedgerConflictError, match="without metadata"):
        portfolio_ledger_document(path)


def test_reader_preserves_callers_snapshot_while_another_connection_appends(tmp_path):
    from contextlib import closing

    from baibai_engine.appdb.read import connect_read_only
    from baibai_engine.position.ledger_read import load_ledger_in_transaction
    from baibai_engine.read_api.position import portfolio_ledger_document

    path = tmp_path / "app.sqlite"
    source = _store_fixture()
    seed_ledger(path, source)
    with sqlite3.connect(path) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
    event = ContributionEvent(
        type="contribution",
        event_id="concurrent-contribution",
        occurred_at=source.as_of.isoformat(),
        amount_yen=100,
    )
    replacement = source.model_copy(update={"events": (*source.events, event)})
    with closing(connect_read_only(path)) as reader:
        reader.execute("BEGIN")
        before, head = load_ledger_in_transaction(reader)
        LedgerStoreService(path).apply_document(
            expected_head=head,
            expected_document=source,
            replacement=replacement,
        )
        assert load_ledger_in_transaction(reader) == (before, head)
        assert reader.in_transaction
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
    assert LedgerStoreService(path).load_with_head() == (replacement, head + 1)
    assert portfolio_ledger_document(path) == replacement
