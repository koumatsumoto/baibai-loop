"""Canonical judgment of whether each Review Set entry merits research time."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.foundation.research_triage import (
    RESEARCH_TRIAGE_CONTRACT_ID,
    RESEARCH_TRIAGE_SCHEMA_VERSION,
    ResearchTriage,
    ResearchTriageCandidateSnapshot,
    ResearchTriageEntry,
)
from baibai_engine.read_api.macro import latest_macro_context_payload, macro_context_payload
from baibai_engine.read_api.research_triage import research_triage_payload_hash
from baibai_engine.screening.discovery.review_set import PublishedReviewSet


class ResearchTriageConflictError(ValueError):
    pass


def latest_research_triage_id(db_path: Path | None = None) -> str | None:
    app_db_path = database_path(db_path)
    if not app_db_path.is_file():
        return None
    from baibai_engine.read_api.research_triage import latest_research_triage_payload

    payload = latest_research_triage_payload(app_db_path)
    return str(payload["research_triage_id"]) if payload is not None else None


class ResearchTriageService:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def publish(
        self,
        triage: ResearchTriage,
        *,
        review_set: PublishedReviewSet,
    ) -> ResearchTriage:
        app_db_path = database_path(self._db_path)
        latest_context = latest_macro_context_payload(app_db_path, as_of=triage.as_of)
        if triage.macro_context_id is None:
            if latest_context is not None:
                raise ResearchTriageConflictError(
                    f"latest eligible macro context must be bound: {latest_context['context_id']}"
                )
        else:
            try:
                macro_context_payload(
                    app_db_path,
                    context_id=triage.macro_context_id,
                    as_of=triage.as_of,
                )
            except ValueError as error:
                raise ResearchTriageConflictError(str(error)) from error
        if triage.review_set_id != review_set.review_set_id:
            raise ResearchTriageConflictError("research triage review-set binding differs")
        if triage.run_revision_id != review_set.run_revision_id:
            raise ResearchTriageConflictError("research triage run binding differs")
        if triage.as_of != review_set.as_of:
            raise ResearchTriageConflictError("research triage as-of differs")
        if triage.screening_rules_hash != review_set.screening_rules_hash:
            raise ResearchTriageConflictError("research triage rules identity differs")
        if triage.candidate_discovery_method != review_set.method:
            raise ResearchTriageConflictError("research triage method identity differs")
        by_ticker = {entry.ticker: entry for entry in review_set.entries}
        if {entry.ticker for entry in triage.entries} != set(by_ticker):
            raise ResearchTriageConflictError("triage entries must equal the Review Set")
        entries = []
        for entry in triage.entries:
            source = by_ticker[entry.ticker]
            entries.append(
                entry.model_copy(
                    update={
                        "candidate_snapshot": ResearchTriageCandidateSnapshot(
                            name=source.name,
                            sector_33=source.sector_33,
                            review_position=source.review_position,
                            nominations=source.nominations,
                            analysis=source.analysis,
                        )
                    }
                )
            )
        triage = triage.model_copy(update={"entries": tuple(entries)})
        initialize_database(self._db_path)
        payload = canonical_json(triage.model_dump(mode="json"))
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT payload FROM research_triage WHERE research_triage_id = ?",
                    (triage.research_triage_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) == payload:
                        connection.rollback()
                        return triage
                    raise ResearchTriageConflictError("research triage identity already differs")
                latest = connection.execute(
                    "SELECT research_triage_id FROM research_triage "
                    "ORDER BY as_of DESC, published_at DESC, research_triage_id DESC LIMIT 1"
                ).fetchone()
                latest_id = str(latest[0]) if latest is not None else None
                if triage.expected_prior_research_triage_id != latest_id:
                    raise ResearchTriageConflictError("research triage expected prior ID is stale")
                connection.execute(
                    "INSERT INTO research_triage "
                    "(research_triage_id, review_set_id, run_revision_id, "
                    "as_of, published_at, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        triage.research_triage_id,
                        triage.review_set_id,
                        triage.run_revision_id,
                        triage.as_of.isoformat(),
                        triage.published_at.isoformat(),
                        payload,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return triage


__all__ = [
    "RESEARCH_TRIAGE_CONTRACT_ID",
    "RESEARCH_TRIAGE_SCHEMA_VERSION",
    "ResearchTriage",
    "ResearchTriageCandidateSnapshot",
    "ResearchTriageConflictError",
    "ResearchTriageEntry",
    "ResearchTriageService",
    "latest_research_triage_id",
    "research_triage_payload_hash",
]
