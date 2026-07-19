"""Database-backed application source implementations."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from baibai_app.sources.types import CandidatesRun, MacroGroupConfig, MacroSeriesConfig, TaskRecord
from baibai_engine.read_api import (
    latest_macro_context_payload,
    list_macro_context_payloads,
    list_reviewed_shortlist_payloads,
    list_task_payloads,
    macro_indicator_series,
    screening_run_payload,
    screening_run_payloads,
    screening_selection_payloads,
    task_store_exists,
)


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


class _SeriesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)


class _GroupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    series: tuple[_SeriesConfig, ...] = Field(min_length=1)


class _DashboardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groups: tuple[_GroupConfig, ...] = Field(min_length=1)


def load_macro_dashboard_config(path: Path) -> tuple[MacroGroupConfig, ...]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = _DashboardConfig.model_validate(raw)
    identifiers = [item.id for group in config.groups for item in group.series]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("macro dashboard series IDs must be unique")
    return tuple(
        MacroGroupConfig(
            title=group.title,
            series=tuple(
                MacroSeriesConfig(series_id=item.id, label=item.label) for item in group.series
            ),
        )
        for group in config.groups
    )


class DbMacroSource:
    def __init__(
        self,
        app_db_path: Path,
        indicators_db_path: Path,
        groups: tuple[MacroGroupConfig, ...],
    ) -> None:
        self._app_db_path = app_db_path.resolve()
        self._indicators_db_path = indicators_db_path.resolve()
        self.groups = groups

    def context(self, *, as_of: date) -> dict[str, object] | None:
        return latest_macro_context_payload(self._app_db_path, as_of=as_of)

    def contexts(self) -> list[dict[str, object]]:
        return list_macro_context_payloads(self._app_db_path)

    def series(self, series_id: str) -> dict[str, object] | None:
        return macro_indicator_series(self._indicators_db_path, series_id=series_id)


class DbCandidatesSource:
    def __init__(self, runs_db_path: Path, app_db_path: Path) -> None:
        self._runs_path = runs_db_path.resolve()
        self._app_path = app_db_path.resolve()

    def latest_run(self) -> CandidatesRun | None:
        raw = screening_run_payload(self._runs_path)
        return None if raw is None else self._parse_run(raw)

    def run(self, run_revision_id: str) -> CandidatesRun | None:
        raw = screening_run_payload(
            self._runs_path,
            run_revision_id=run_revision_id,
        )
        return None if raw is None else self._parse_run(raw)

    def publications(self) -> list[dict[str, object]]:
        return screening_run_payloads(self._runs_path)

    def selections(self, *, run_revision_id: str | None = None) -> list[dict[str, object]]:
        return screening_selection_payloads(
            self._runs_path,
            run_revision_id=run_revision_id,
        )

    def reviewed_shortlists(self) -> list[dict[str, object]]:
        return list_reviewed_shortlist_payloads(self._app_path)

    @staticmethod
    def _parse_run(raw: dict[str, object]) -> CandidatesRun:
        candidates = raw["candidates"]
        if not isinstance(candidates, list) or not all(
            isinstance(item, dict) for item in candidates
        ):
            raise ValueError("screening candidates must be an array of objects")
        return CandidatesRun(
            run_id=str(raw["public_run_id"]),
            run_date=date.fromisoformat(str(raw["run_date"])),
            asof_date=date.fromisoformat(str(raw["as_of_date"])),
            universe_size=int(str(raw["universe_size"])),
            source_path=str(raw["run_revision_id"]),
            rows=tuple(candidates),
        )


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_date(value: object) -> date | None:
    return None if value is None else date.fromisoformat(str(value))
