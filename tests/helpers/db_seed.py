"""Test-only database seed helpers; production exposes no bulk-import writer."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

from baibai_engine.primitives.json import canonical_json
from baibai_engine.investment_judgment.operation.models import OperationSession
from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument
from baibai_engine.portfolio.ledger.read import LedgerConflictError
from baibai_engine.portfolio.ledger.store import LedgerStoreService, _event_values
from baibai_engine.storage.application.write import connect_rw, initialize_database
from baibai_engine.tasks.models import Task


def seed_ledger(path: Path, document: PortfolioLedgerDocument) -> None:
    initialize_database(path)
    if not document.events:
        raise LedgerConflictError("test ledger requires an opening event")
    initial = document.model_copy(update={"events": (document.events[0],)})
    payload = canonical_json(initial.model_dump(mode="json"))
    with closing(connect_rw(path)) as connection:
        if connection.execute("SELECT 1 FROM ledger_meta WHERE singleton = 1").fetchone():
            if LedgerStoreService(path).load() != document:
                raise LedgerConflictError("test ledger seed differs from existing state")
            return
        connection.execute(
            """
            INSERT INTO ledger_meta(
                singleton, schema_version, portfolio_scope, as_of,
                estimated_exit_tax_rate_bps, estimated_exit_tax_basis, payload
            ) VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            (
                initial.schema_version,
                initial.portfolio_scope,
                initial.as_of.isoformat(),
                initial.estimated_exit_tax_rate_bps,
                initial.estimated_exit_tax_basis,
                payload,
            ),
        )
        connection.execute(
            """
            INSERT INTO ledger_event(
                append_seq, event_id, occurred_at, same_instant_order,
                event_type, ticker, decision_reference, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _event_values(1, document.events[0], 0),
        )
    LedgerStoreService(path).apply_document(
        expected_head=1,
        expected_document=initial,
        replacement=document,
    )


def seed_tasks(path: Path, tasks: Iterable[Task]) -> None:
    initialize_database(path)
    with closing(connect_rw(path)) as connection:
        connection.executemany(
            """
            INSERT INTO task(
                task_id, status, kind, ticker, due_date, event_date,
                created_at, closed_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    task.task_id,
                    task.status,
                    task.kind,
                    task.ticker,
                    task.due_date.isoformat(),
                    None if task.event_date is None else task.event_date.isoformat(),
                    task.created_at.isoformat(),
                    None if task.closed_at is None else task.closed_at.isoformat(),
                    canonical_json(task.payload()),
                )
                for task in tasks
            ],
        )


def seed_historical_operation(path: Path, operation: OperationSession) -> None:
    """保存済み歴史Operationの読取fixture。現行のstart検証を代行しない。"""
    initialize_database(path)
    with closing(connect_rw(path)) as connection, connection:
        connection.execute(
            "INSERT INTO operation_session "
            "(operation_id,session_kind,status,as_of,ticker,started_at,completed_at,payload) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                operation.operation_id,
                operation.session_kind,
                operation.status,
                operation.as_of.isoformat(),
                operation.ticker,
                operation.started_at.isoformat(),
                None if operation.completed_at is None else operation.completed_at.isoformat(),
                canonical_json(operation.payload.model_dump(mode="json")),
            ),
        )
