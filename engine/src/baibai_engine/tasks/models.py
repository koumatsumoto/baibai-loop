"""Write-time contract for operational tasks."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TaskKind = Literal["earnings-review", "ops", "follow-up", "other"]
TaskStatus = Literal["open", "done", "dropped"]


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(pattern=r"^task-[0-9]{8}-[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1)
    kind: TaskKind
    status: TaskStatus
    ticker: str | None = Field(default=None, pattern=r"^[0-9]{4}[A-Z0-9]?$")
    due_date: date
    event_label: str | None = None
    event_date: date | None = None
    body_md: str | None = None
    related_refs: tuple[str, ...] = ()
    created_at: date
    closed_at: date | None = None

    def payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")
