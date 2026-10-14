"""Publish a canonical ResearchTriage bound to one immutable Review Set."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.repository_layout import APPLICATION_DB_PATH
from baibai_engine.foundation.time import JST
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market_context.assessment.models import ASSESSMENT_STALE_DAYS
from baibai_engine.market_context.assessment.query import latest_assessment_payload
from baibai_engine.screening.discovery.contracts import PublishedReviewSet
from baibai_engine.screening.run_store import ScreeningRunReader

from .contracts import ResearchTriage
from .query import latest_research_triage_id
from .service import ResearchTriageConflictError, ResearchTriageService


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
        latest_context = latest_assessment_payload(
            app_db_path or APPLICATION_DB_PATH,
            as_of=date.fromisoformat(as_of),
            known_at=datetime.now(JST),
        )
        if latest_context is not None:
            context_asof = date.fromisoformat(str(latest_context["as_of"]))
            age_days = (date.fromisoformat(as_of) - context_asof).days
            if age_days > ASSESSMENT_STALE_DAYS:
                print(
                    "warning: latest eligible market context assessment is stale: "
                    f"{latest_context['context_id']} age_days={age_days}",
                    file=sys.stderr,
                )
        compact_as_of = as_of.replace("-", "")
        draft_entries: list[dict[str, object]] = []
        for source in review_set.entries:
            nominations = source.nominations
            approaches = ",".join(item.valuation_approach_id for item in nominations)
            analysis = source.analysis.model_dump(mode="json")
            status = source.analysis.context.next_earnings_status
            # The draft is a human input. Publication still binds the original
            # frozen Review Set; a legacy date-only status must not claim disclosure.
            analysis["context"]["next_earnings_status"] = (
                "scheduled_date_reached" if status == "announced" else status
            )
            draft_entries.append(
                {
                    "ticker": source.ticker,
                    "decision": "TODO",
                    "priority": None,
                    "rationale": f"TODO — approaches={approaches}",
                    "research_question": None,
                    "key_risk": None,
                    "candidate_snapshot": {
                        "name": source.name,
                        "sector_33": source.sector_33,
                        "nominations": [item.model_dump(mode="json") for item in nominations],
                        "analysis": analysis,
                    },
                }
            )
        draft = {
            "schema_version": 4,
            "kind": "research_triage",
            "research_triage_id": f"research-triage-{compact_as_of}-<slug>",
            "review_set_id": review_set.review_set_id,
            "run_revision_id": review_set.run_revision_id,
            "as_of": as_of,
            "published_at": "<JST publication timestamp>",
            "market_context_assessment_id": (
                None if latest_context is None else str(latest_context["context_id"])
            ),
            "expected_prior_research_triage_id": latest_research_triage_id(app_db_path),
            "screening_rules_hash": review_set.screening_rules_hash,
            "candidate_discovery_method": review_set.method.model_dump(mode="json"),
            "triage_contract_id": "research-triage-v4",
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baibai-engine research triage")
    triage_commands = parser.add_subparsers(dest="research_triage_command", required=True)
    triage_publish = triage_commands.add_parser(
        "publish",
        help="publish a ResearchTriage draft bound to an immutable Review Set",
    )
    triage_publish.add_argument("draft")
    triage_publish.add_argument("--db", help="application DB path")
    triage_publish.add_argument("--runs-db", help="screening run store path")
    triage_scaffold = triage_commands.add_parser(
        "scaffold",
        help="create a fail-closed draft with Review Set machine coordinates",
    )
    triage_scaffold.add_argument("--review-set-id", required=True)
    triage_scaffold.add_argument("--runs-db", help="screening run store path")
    triage_scaffold.add_argument("--output-path", required=True)
    triage_scaffold.add_argument("--db", help="application DB path")
    triage_scaffold.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing --output-path file",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.research_triage_command == "scaffold":
        return scaffold_research_triage(
            args.review_set_id,
            output_path=Path(args.output_path),
            app_db_path=Path(args.db) if args.db else None,
            runs_db_path=Path(args.runs_db) if args.runs_db else None,
            force=args.force,
        )
    return publish_research_triage(
        Path(args.draft),
        app_db_path=Path(args.db) if args.db else None,
        runs_db_path=Path(args.runs_db) if args.runs_db else None,
    )
