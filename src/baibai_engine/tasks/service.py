"""Task application service; the only task mutation boundary."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any, Literal

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database

from .models import Task, TaskKind, TaskStatus


class TaskNotFoundError(ValueError):
    pass


class TaskConflictError(ValueError):
    pass


class TaskService:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def add(
        self,
        *,
        title: str,
        kind: TaskKind,
        due_date: date,
        created_at: date,
        ticker: str | None = None,
        event_date: date | None = None,
        event_label: str | None = None,
        body_md: str | None = None,
        related_refs: tuple[str, ...] = (),
    ) -> Task:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            task_id = self._next_id(
                connection,
                created_at=created_at,
                slug_source=ticker or kind,
            )
            task = Task(
                task_id=task_id,
                title=title,
                kind=kind,
                status="open",
                ticker=ticker,
                due_date=due_date,
                event_date=event_date,
                event_label=event_label,
                body_md=body_md,
                related_refs=related_refs,
                created_at=created_at,
            )
            self._insert(connection, task)
            return task

    def list(self, *, status: TaskStatus | None = None) -> list[Task]:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            sql = "SELECT payload FROM task"
            parameters: tuple[object, ...] = ()
            if status is not None:
                sql += " WHERE status = ?"
                parameters = (status,)
            sql += " ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, due_date, task_id"
            rows = connection.execute(sql, parameters).fetchall()
        return [Task.model_validate_json(str(row[0])) for row in rows]

    def close(self, task_id: str, *, status: Literal["done", "dropped"], closed_at: date) -> Task:
        task = self.get(task_id)
        updated = task.model_copy(update={"status": status, "closed_at": closed_at})
        self._replace(task, updated)
        return updated

    def edit(self, task_id: str, changes: Mapping[str, object]) -> Task:
        allowed = {
            "title",
            "kind",
            "ticker",
            "due_date",
            "event_label",
            "event_date",
            "body_md",
            "related_refs",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"task fields cannot be edited: {', '.join(sorted(unknown))}")
        if not changes:
            raise ValueError("edit requires at least one changed field")
        task = self.get(task_id)
        updated = Task.model_validate({**task.payload(), **changes})
        self._replace(task, updated)
        return updated

    def get(self, task_id: str) -> Task:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            row = connection.execute(
                "SELECT payload FROM task WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"unknown task_id: {task_id}")
        return Task.model_validate_json(str(row[0]))

    def import_tasks(self, tasks: Iterable[Task]) -> tuple[int, int]:
        """Create-only all-or-nothing import; identical rows are no-change."""
        validated = list(tasks)
        identifiers = [task.task_id for task in validated]
        if len(identifiers) != len(set(identifiers)):
            raise TaskConflictError("task_id must be unique in import input")
        initialize_database(self._db_path)
        inserted = 0
        unchanged = 0
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for task in validated:
                    payload = canonical_json(task.payload())
                    row = connection.execute(
                        "SELECT payload FROM task WHERE task_id = ?", (task.task_id,)
                    ).fetchone()
                    if row is None:
                        self._insert(connection, task)
                        inserted += 1
                    elif str(row[0]) == payload:
                        unchanged += 1
                    else:
                        raise TaskConflictError(
                            f"task differs from existing canonical row: {task.task_id}"
                        )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return inserted, unchanged

    def _replace(self, before: Task, after: Task) -> None:
        initialize_database(self._db_path)
        before_payload = canonical_json(before.payload())
        after_payload = canonical_json(after.payload())
        with closing(connect_rw(self._db_path)) as connection:
            cursor = connection.execute(
                """
                UPDATE task
                SET status = ?, kind = ?, ticker = ?, due_date = ?, event_date = ?,
                    created_at = ?, closed_at = ?, payload = ?
                WHERE task_id = ? AND payload = ?
                """,
                (*_columns(after), after_payload, after.task_id, before_payload),
            )
            if cursor.rowcount != 1:
                raise TaskConflictError(f"task changed while editing: {after.task_id}")

    @staticmethod
    def _insert(connection: sqlite3.Connection, task: Task) -> None:
        connection.execute(
            """
            INSERT INTO task (
                task_id, status, kind, ticker, due_date, event_date, created_at, closed_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task.task_id, *_columns(task), canonical_json(task.payload())),
        )

    @staticmethod
    def _next_id(connection: sqlite3.Connection, *, created_at: date, slug_source: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", slug_source.lower()).strip("-") or "task"
        prefix = f"task-{created_at:%Y%m%d}-{slug}"
        candidate = prefix
        suffix = 2
        while connection.execute("SELECT 1 FROM task WHERE task_id = ?", (candidate,)).fetchone():
            candidate = f"{prefix}-{suffix}"
            suffix += 1
        return candidate


def _columns(task: Task) -> tuple[Any, ...]:
    return (
        task.status,
        task.kind,
        task.ticker,
        task.due_date.isoformat(),
        None if task.event_date is None else task.event_date.isoformat(),
        task.created_at.isoformat(),
        None if task.closed_at is None else task.closed_at.isoformat(),
    )
