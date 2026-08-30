"""Publish a canonical ResearchTriage bound to one immutable Review Set."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.screening.run_store import ScreeningRunReader

from .research_triage import (
    ResearchTriage,
    ResearchTriageConflictError,
    ResearchTriageService,
)


def publish_research_triage(
    draft_path: Path,
    *,
    app_db_path: Path | None = None,
    runs_db_path: Path | None = None,
) -> int:
    try:
        raw = safe_load(draft_path.read_text(encoding="utf-8"))
        triage = ResearchTriage.model_validate(raw)
        source = ScreeningRunReader(runs_db_path).get_review_set(triage.review_set_id)
        if source is None:
            raise ResearchTriageConflictError(
                f"source Review Set is unavailable: {triage.review_set_id}"
            )
        published = ResearchTriageService(app_db_path).publish(
            triage,
            review_set=source.payload,
        )
    except (OSError, ValueError, ValidationError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    yaml.safe_dump(
        published.model_dump(mode="json"), sys.stdout, sort_keys=False, allow_unicode=True
    )
    return 0


__all__ = ["publish_research_triage"]
