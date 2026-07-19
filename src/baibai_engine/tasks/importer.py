"""Legacy task-list loader used only by the migration runner."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from baibai_engine.foundation.yaml_io import safe_load

from .models import Task
from .service import TaskService


def import_task_file(source: Path, *, db_path: Path | None = None) -> tuple[int, int]:
    raw = safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise ValueError("task list must be a schema_version 1 mapping")
    items = raw.get("tasks")
    if not isinstance(items, list):
        raise ValueError("task list tasks must be an array")
    tasks = [Task.model_validate(item) for item in items]
    return TaskService(db_path).import_tasks(tasks)
