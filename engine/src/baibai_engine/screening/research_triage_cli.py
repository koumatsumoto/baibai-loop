"""Publish a canonical ResearchTriage bound to one immutable Review Set."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Mapping
from datetime import date
from pathlib import Path

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.repository_layout import APPLICATION_DB_PATH
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.read_api.macro import MACRO_CONTEXT_STALE_DAYS, latest_macro_context_payload
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


def scaffold_research_triage(
    review_set_path: Path,
    *,
    output_path: Path,
    app_db_path: Path | None = None,
    force: bool = False,
) -> int:
    """Create one fail-closed draft with every Review Set coordinate in place."""

    if output_path.exists() and not force:
        print(f"output already exists: {output_path}", file=sys.stderr)
        return 1
    try:
        review_set = safe_load(review_set_path.read_text(encoding="utf-8"))
        if not isinstance(review_set, Mapping):
            raise ValueError("Review Set output must be a mapping")
        entries = review_set.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ValueError("Review Set output must contain entries")
        as_of = str(review_set.get("as_of") or "")
        latest_context = latest_macro_context_payload(
            app_db_path or APPLICATION_DB_PATH,
            as_of=date.fromisoformat(as_of),
        )
        if latest_context is not None:
            context_asof = date.fromisoformat(str(latest_context["as_of"]))
            age_days = (date.fromisoformat(as_of) - context_asof).days
            if age_days > MACRO_CONTEXT_STALE_DAYS:
                print(
                    "warning: latest eligible macro context is stale: "
                    f"{latest_context['context_id']} age_days={age_days}",
                    file=sys.stderr,
                )
        compact_as_of = as_of.replace("-", "")
        review_basis = review_set.get("review_basis")
        judged_through = (
            review_basis.get("judged_through_research_triage_id")
            if isinstance(review_basis, Mapping)
            else None
        )
        draft_entries: list[dict[str, object]] = []
        for source in entries:
            if not isinstance(source, Mapping):
                raise ValueError("Review Set entry must be a mapping")
            analysis = source.get("analysis")
            analysis_map = analysis if isinstance(analysis, Mapping) else {}
            nominations = source.get("nominations")
            if not isinstance(nominations, list):
                raise ValueError("Review Set nominations must be a list")
            approaches = ",".join(
                str(item.get("valuation_approach_id"))
                for item in nominations
                if isinstance(item, Mapping)
            )
            draft_entries.append(
                {
                    "ticker": source.get("ticker"),
                    "decision": "TODO",
                    "priority": None,
                    "rationale": (
                        "TODO — "
                        f"review_position={source.get('review_position')}; "
                        f"support_count={source.get('support_count')}; "
                        f"approaches={approaches}"
                    ),
                    "research_question": None,
                    "key_risk": None,
                    "machine_snapshot": {
                        "review_position": source.get("review_position"),
                        "nominations": nominations,
                        "support_count": source.get("support_count"),
                        "expected_return": analysis_map.get("expected_return"),
                        "fair_value": None,
                        "data_quality": analysis_map.get("data_quality"),
                    },
                }
            )
        draft = {
            "schema_version": 1,
            "kind": "research_triage",
            "research_triage_id": f"research-triage-{compact_as_of}-<slug>",
            "review_set_id": review_set.get("review_set_id"),
            "run_revision_id": review_set.get("run_revision_id"),
            "as_of": as_of,
            "published_at": "<JST publication timestamp>",
            "macro_context_id": (
                None if latest_context is None else str(latest_context["context_id"])
            ),
            "review_basis_research_triage_id": judged_through,
            "triage_contract_id": "research-triage-v1",
            "entries": draft_entries,
        }
        rendered = yaml.safe_dump(draft, sort_keys=False, allow_unicode=True)
        write_text_atomic(output_path, rendered)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(rendered, end="")
    return 0


__all__ = ["publish_research_triage", "scaffold_research_triage"]
