"""Publish a canonical ResearchTriage bound to one immutable Review Set."""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.repository_layout import APPLICATION_DB_PATH
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.read_api.macro import MACRO_CONTEXT_STALE_DAYS, latest_macro_context_payload
from baibai_engine.screening.discovery.review_set import PublishedReviewSet
from baibai_engine.screening.run_store import ScreeningRunReader

from .research_triage import (
    ResearchTriage,
    ResearchTriageConflictError,
    ResearchTriageService,
    latest_research_triage_id,
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
        review_set = PublishedReviewSet.model_validate(source.payload)
        published = ResearchTriageService(app_db_path).publish(triage, review_set=review_set)
    except (OSError, ValueError, ValidationError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    yaml.safe_dump(
        published.model_dump(mode="json"), sys.stdout, sort_keys=False, allow_unicode=True
    )
    return 0


def scaffold_research_triage(
    review_set_id: str,
    *,
    output_path: Path,
    app_db_path: Path | None = None,
    runs_db_path: Path | None = None,
    force: bool = False,
) -> int:
    """Create one fail-closed draft with every Review Set coordinate in place."""

    if output_path.exists() and not force:
        print(f"output already exists: {output_path}", file=sys.stderr)
        return 1
    try:
        publication = ScreeningRunReader(runs_db_path).get_review_set(review_set_id)
        if publication is None:
            raise ValueError(f"source Review Set is unavailable: {review_set_id}")
        review_set = PublishedReviewSet.model_validate(publication.payload)
        if not review_set.entries:
            raise ValueError("Review Set must contain entries")
        as_of = review_set.as_of.isoformat()
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
        draft_entries: list[dict[str, object]] = []
        for source in review_set.entries:
            nominations = source.nominations
            approaches = ",".join(item.valuation_approach_id for item in nominations)
            draft_entries.append(
                {
                    "ticker": source.ticker,
                    "decision": "TODO",
                    "priority": None,
                    "rationale": (
                        "TODO — "
                        f"review_position={source.review_position}; "
                        f"support_count={len(nominations)}; "
                        f"approaches={approaches}"
                    ),
                    "research_question": None,
                    "key_risk": None,
                    "candidate_snapshot": {
                        "name": source.name,
                        "sector_33": source.sector_33,
                        "review_position": source.review_position,
                        "nominations": [item.model_dump(mode="json") for item in nominations],
                        "analysis": source.analysis.model_dump(mode="json"),
                    },
                }
            )
        draft = {
            "schema_version": 2,
            "kind": "research_triage",
            "research_triage_id": f"research-triage-{compact_as_of}-<slug>",
            "review_set_id": review_set.review_set_id,
            "run_revision_id": review_set.run_revision_id,
            "as_of": as_of,
            "published_at": "<JST publication timestamp>",
            "macro_context_id": (
                None if latest_context is None else str(latest_context["context_id"])
            ),
            "expected_prior_research_triage_id": latest_research_triage_id(app_db_path),
            "screening_rules_hash": review_set.screening_rules_hash,
            "candidate_discovery_method": review_set.method.model_dump(mode="json"),
            "triage_contract_id": "research-triage-v2",
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
