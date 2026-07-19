"""Explicit reviewed-shortlist publication command."""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.screening.run_store import ScreeningRunReader

from .shortlist import (
    ReviewedShortlist,
    ReviewedShortlistService,
    SelectionBinding,
    ShortlistConflictError,
)


def publish_shortlist(
    draft_path: Path,
    *,
    app_db_path: Path | None = None,
    runs_db_path: Path | None = None,
) -> int:
    try:
        raw = safe_load(draft_path.read_text(encoding="utf-8"))
        shortlist = ReviewedShortlist.model_validate(raw)
        reader = ScreeningRunReader(runs_db_path)
        selection = reader.get_selection(shortlist.selection_id)
        if selection is None:
            raise ShortlistConflictError(
                f"source selection is unavailable: {shortlist.selection_id}"
            )
        run = reader.get_run(selection.run_revision_id)
        if run is None:  # pragma: no cover - run-store FK invariant
            raise ShortlistConflictError(f"source run is unavailable: {selection.run_revision_id}")
        binding = SelectionBinding(
            selection_id=selection.selection_id,
            run_revision_id=selection.run_revision_id,
            as_of=date.fromisoformat(selection.as_of_date),
            profile=selection.profile,
            macro_context_id=selection.macro_context_id,
            candidate_tickers=frozenset(str(item["ticker"]) for item in run.candidates),
        )
        published = ReviewedShortlistService(app_db_path).publish(
            shortlist,
            selection=binding,
        )
    except (OSError, ValueError, ValidationError, ShortlistConflictError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    yaml.safe_dump(published.payload(), sys.stdout, sort_keys=False, allow_unicode=True)
    return 0


__all__ = ["publish_shortlist"]
