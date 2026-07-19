from __future__ import annotations

import sqlite3
from dataclasses import astuple
from datetime import timedelta
from pathlib import Path

import pytest

from baibai_engine.appdb.write import initialize_database
from baibai_engine.position.importer import import_and_check_ledger
from baibai_engine.position.ledger import (
    ContributionEvent,
    ReservationEvent,
    load_portfolio_ledger,
)
from baibai_engine.position.store import LedgerConflictError, LedgerStoreService

FIXTURE = Path("tests/fixtures/portfolio-ledger/representative.yaml")
CANONICAL = Path("records/04-position/portfolio-ledger.yaml")


def _create_schema(path: Path) -> None:
    initialize_database(path)


@pytest.mark.parametrize("source_path", [FIXTURE, CANONICAL])
def test_document_import_is_exact_idempotent_and_has_full_parity(
    tmp_path: Path, source_path: Path
) -> None:
    path = tmp_path / "app.sqlite"
    _create_schema(path)
    first, parity = import_and_check_ledger(source_path, db_path=path)
    second, second_parity = import_and_check_ledger(source_path, db_path=path)
    source = load_portfolio_ledger(source_path)

    assert astuple(first) == (
        len(source.events),
        0,
        len(source.market_prices),
        0,
        1,
        0,
    )
    assert astuple(second) == (
        0,
        len(source.events),
        0,
        len(source.market_prices),
        0,
        1,
    )
    assert parity.matches
    assert second_parity.matches
    assert LedgerStoreService(path).load() == source

    with sqlite3.connect(path) as connection:
        assert [
            row[0]
            for row in connection.execute("SELECT event_id FROM ledger_event ORDER BY append_seq")
        ] == [event.event_id for event in source.events]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_import_conflict_rolls_back_all_missing_rows(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _create_schema(path)
    source = load_portfolio_ledger(FIXTURE)
    service = LedgerStoreService(path)
    service.import_document(source)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE ledger_event SET payload = json_set(payload, '$.amount_yen', 1) "
            "WHERE append_seq = 1"
        )
        connection.execute("DELETE FROM ledger_event WHERE append_seq = 2")

    with pytest.raises(LedgerConflictError, match="conflicts"):
        service.import_document(source)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM ledger_event").fetchone()[0] == (
            len(source.events) - 1
        )


def test_late_event_is_physically_appended_but_replayed_by_time(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _create_schema(path)
    source = load_portfolio_ledger(FIXTURE)
    service = LedgerStoreService(path)
    service.import_document(source)
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
    source = load_portfolio_ledger(FIXTURE)
    service = LedgerStoreService(path)
    service.import_document(source)
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
    source = load_portfolio_ledger(FIXTURE)
    service = LedgerStoreService(path)
    service.import_document(source)
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


def test_proposal_id_is_relational_only_for_db_native_reference(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _create_schema(path)
    source = load_portfolio_ledger(FIXTURE)
    service = LedgerStoreService(path)
    service.import_document(source)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO research_packet(
                packet_id, ticker, as_of, recommendation, published_at,
                supersedes_id, payload
            ) VALUES ('packet-proposal-test', '2331', '2026-07-19', 'buy',
                      '2026-07-19T09:00:00+09:00', NULL, '{}')
            """
        )
        connection.execute(
            """
            INSERT INTO research_review(review_id, packet_id, reviewed_at, payload)
            VALUES ('review-proposal-test', 'packet-proposal-test',
                    '2026-07-19T09:00:00+09:00', '{}')
            """
        )
        connection.execute(
            """
            INSERT INTO proposal(
                proposal_id, ticker, packet_id, review_id, created_at,
                status, decided_at, payload
            ) VALUES ('prop-20260719-2331-1', '2331', 'packet-proposal-test',
                      'review-proposal-test', '2026-07-19T09:00:00+09:00',
                      'approved', '2026-07-19T09:01:00+09:00', '{}')
            """
        )
    template = next(item for item in source.events if isinstance(item, ReservationEvent))
    native = template.model_copy(
        update={
            "event_id": "human-open-proposal",
            "reservation_id": "reservation-proposal",
            "order_id": "order-proposal",
            "decision_reference": "prop-20260719-2331-1",
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
                "SELECT proposal_id FROM ledger_event WHERE event_id = ?",
                (native.event_id,),
            ).fetchone()[0]
            == "prop-20260719-2331-1"
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM ledger_event "
                "WHERE json_extract(payload, '$.decision_reference') LIKE 'https://%' "
                "AND proposal_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
