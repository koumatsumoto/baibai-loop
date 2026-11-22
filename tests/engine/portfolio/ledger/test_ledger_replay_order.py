"""The same-time replay index cannot rewrite recorded FIFO chronology."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from tests.helpers.db_seed import seed_ledger

from baibai_engine.portfolio.ledger.events import (
    ContributionEvent,
    ExecutionEvent,
    PortfolioLedgerDocument,
    replay_events_through,
)
from baibai_engine.portfolio.ledger.read import LedgerConflictError, load_ledger_events
from baibai_engine.portfolio.ledger.store import LedgerStoreService
from baibai_engine.storage.sqlite.application.read import connect_read_only

SALE = "2025-04-09T10:00:00+09:00"
AS_OF = "2025-04-10T17:00:00+09:00"
FILL_TIMES = ("2025-04-04T01:00:00+00:00", "2025-04-04T10:00:00+09:00")
CHEAP, DEAR = "fill-z-cheap", "fill-a-dear"


def _document(*, reversed_order: bool = False) -> PortfolioLedgerDocument:
    reservations = []
    fills = []
    for identity, time, price in zip((CHEAP, DEAR), FILL_TIMES, ("1000", "1200"), strict=True):
        reservations.append(
            {
                "event_id": f"order-{identity}",
                "type": "reservation",
                "occurred_at": "2025-04-03T10:00:00+09:00",
                "reservation_id": f"reserve-{identity}",
                "order_id": f"broker-{identity}",
                "ticker": "1234",
                "sector": "synthetic",
                "quantity": 100,
                "price_guard_yen": price,
            }
        )
        fills.append(
            {
                "event_id": identity,
                "type": "execution",
                "occurred_at": time,
                "execution_id": f"execute-{identity}",
                "reservation_id": f"reserve-{identity}",
                "ticker": "1234",
                "side": "buy",
                "quantity": 100,
                "price_yen": price,
            }
        )
    return PortfolioLedgerDocument.model_validate(
        {
            "schema_version": 3,
            "portfolio_scope": "repository_only",
            "as_of": AS_OF,
            "events": [
                {
                    "event_id": "opening",
                    "type": "opening_balance",
                    "occurred_at": "2025-04-03T09:00:00+09:00",
                    "amount_yen": 1_000_000,
                },
                *reservations,
                *(reversed(fills) if reversed_order else fills),
                {
                    "event_id": "sold",
                    "type": "execution",
                    "occurred_at": SALE,
                    "execution_id": "sell-fact",
                    "ticker": "1234",
                    "side": "sell",
                    "quantity": 100,
                    "price_yen": "1300",
                },
            ],
        }
    )


def _prefix(path: Path, head: int, *, ids: tuple[str, ...] | None = None):
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        return load_ledger_events(connection, append_head=head, event_ids=ids)


@pytest.mark.parametrize(
    ("reversed_order", "pnl", "cost"), [(False, 30_000, 120_000), (True, 10_000, 100_000)]
)
def test_same_instant_writer_order_and_late_tie_remain_replayable(
    tmp_path, reversed_order, pnl, cost
):
    path = tmp_path / "app.sqlite"
    document = _document(reversed_order=reversed_order)
    seed_ledger(path, document)
    service = LedgerStoreService(path)
    archived = _prefix(path, 5)
    assert archived == document.events[:5]

    # Physically after the sale, chronologically at the tied fills. This is a
    # valid backdated append, not a globally increasing append_seq in replay.
    late = ContributionEvent(
        event_id="later-reported",
        type="contribution",
        occurred_at=FILL_TIMES[0],
        amount_yen=500,
    )
    replacement = document.model_copy(update={"events": (*archived, late, document.events[5])})
    assert (
        service.apply_document(
            expected_head=6, expected_document=document, replacement=replacement
        ).append_head
        == 7
    )
    assert service.load_with_head() == (replacement, 7)
    saved = _prefix(path, 7)
    assert saved == replacement.events
    assert _prefix(path, 5) == archived
    assert _prefix(path, 7, ids=(DEAR, CHEAP, DEAR)) == tuple(
        next(e for e in archived if e.event_id == key) for key in (DEAR, CHEAP, DEAR)
    )
    state = replay_events_through(saved, document.as_of)
    assert state.realized_gross_pnl_yen == pnl
    assert sum(lot.cost_yen for lot in state.lots["1234"]) == cost


def test_inverted_index_rejected_by_full_and_ordered_prefix_reads(tmp_path):
    path = tmp_path / "app.sqlite"
    document = _document()
    seed_ledger(path, document)
    earlier = _prefix(path, 4)
    with closing(sqlite3.connect(path)) as writer, writer:
        for identity, ordinal in ((CHEAP, 99), (DEAR, 0), (CHEAP, 1)):
            writer.execute(
                "UPDATE ledger_event SET same_instant_order=? WHERE event_id=?",
                (ordinal, identity),
            )
        assert writer.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    before = path.read_bytes()

    with pytest.raises(LedgerConflictError, match="same-instant replay order"):
        LedgerStoreService(path).load()
    with pytest.raises(LedgerConflictError, match="same-instant replay order"):
        _prefix(path, 5)
    assert _prefix(path, 4) == earlier
    named = _prefix(path, 5, ids=(DEAR,))
    assert isinstance(named[0], ExecutionEvent)
    assert named[0].price_yen == 1200
    assert path.read_bytes() == before
