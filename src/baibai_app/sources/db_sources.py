"""Database-backed application source implementations."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from baibai_app.sources.types import TaskRecord
from baibai_engine.read_api import list_task_payloads, task_store_exists


class DbTaskSource:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path.resolve()

    def exists(self) -> bool:
        return task_store_exists(self._path)

    def list_tasks(self) -> list[TaskRecord]:
        return [self._parse_task(payload) for payload in list_task_payloads(self._path)]

    @staticmethod
    def _parse_task(raw: dict[str, object]) -> TaskRecord:
        related_refs = raw.get("related_refs", [])
        if not isinstance(related_refs, list):
            raise ValueError("task related_refs must be an array")
        return TaskRecord(
            task_id=str(raw["task_id"]),
            title=str(raw["title"]),
            kind=str(raw["kind"]),
            status=str(raw["status"]),
            ticker=_optional_text(raw.get("ticker")),
            due_date=date.fromisoformat(str(raw["due_date"])),
            event_label=_optional_text(raw.get("event_label")),
            event_date=_optional_date(raw.get("event_date")),
            body_md=_optional_text(raw.get("body_md")),
            related_refs=tuple(str(item) for item in related_refs),
            created_at=date.fromisoformat(str(raw["created_at"])),
            closed_at=_optional_date(raw.get("closed_at")),
        )


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_date(value: object) -> date | None:
    return None if value is None else date.fromisoformat(str(value))
