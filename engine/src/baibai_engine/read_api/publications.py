"""調査工程へ保存publicationの原本とmetadata pageを再評価せず見せる。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from baibai_engine.appdb.read import connect_read_only
from baibai_engine.foundation.sqlite_pages import object_payload, select_page

from .stored import required_read

# Table/column names are domain-owned literals, never caller SQL.
TABLES = {
    "macro_context": (
        "context_id",
        ("as_of", "published_at", "context_id"),
        "context_id, schema_version, as_of, published_at, supersedes_id",
    ),
    "research_triage": (
        "research_triage_id",
        ("as_of", "published_at", "research_triage_id"),
        "research_triage_id, review_set_id, run_revision_id, as_of, published_at",
    ),
    "thesis": (
        "thesis_id",
        ("as_of", "published_at", "thesis_id"),
        "thesis_id, ticker, as_of, recommendation, published_at, supersedes_id, core_sha256",
    ),
    "thesis_review": (
        "review_id",
        ("reviewed_at", "review_id"),
        "review_id, thesis_id, reviewed_at",
    ),
    "capital_allocation_assessment": (
        "capital_allocation_assessment_id",
        ("as_of", "published_at", "capital_allocation_assessment_id"),
        "capital_allocation_assessment_id, as_of, published_at, result, research_triage_id",
    ),
    "position_review": (
        "position_review_id",
        ("as_of", "position_review_id"),
        "position_review_id, ticker, as_of, thesis_id, candidate_thesis_id",
    ),
    "portfolio_outcome": (
        "outcome_id",
        ("period_end_date", "outcome_id"),
        "outcome_id, horizon, period_start_date, period_end_date, status",
    ),
    "task": (
        "task_id",
        ("task_id",),
        "task_id, status, kind, ticker, due_date, event_date, created_at, closed_at",
    ),
    "operation_session": (
        "operation_id",
        ("started_at", "operation_id"),
        "operation_id, session_kind, status, as_of, ticker, started_at, completed_at",
    ),
}


def validate_publication(table: str, row: dict[str, Any]) -> str:
    from baibai_engine.foundation.research_triage import ResearchTriage
    from baibai_engine.macro.context.models import MacroContextDocument
    from baibai_engine.operation.models import OperationSession
    from baibai_engine.position.outcome_models import PortfolioOutcomePayload
    from baibai_engine.research.capital_allocation import CapitalAllocationAssessment
    from baibai_engine.research.position_review import PositionReviewDocument
    from baibai_engine.research.thesis import ThesisDocument, ThesisReview
    from baibai_engine.tasks.models import Task

    models: dict[str, type[BaseModel]] = {
        "macro_context": MacroContextDocument,
        "research_triage": ResearchTriage,
        "thesis": ThesisDocument,
        "thesis_review": ThesisReview,
        "capital_allocation_assessment": CapitalAllocationAssessment,
        "position_review": PositionReviewDocument,
        "portfolio_outcome": PortfolioOutcomePayload,
        "task": Task,
        "operation_session": OperationSession,
    }
    payload = row["payload"]
    identifier = TABLES[table][0]
    identity_payload = payload.get("input_snapshot", {}) if table == "thesis" else payload
    if identifier in payload and payload[identifier] != row[identifier]:
        raise ValueError("publication identity differs")
    if table == "thesis":
        # Historical publications may carry identity at the document root.
        for source in (payload, identity_payload):
            for field in ("ticker", "as_of"):
                if field in source and source[field] != row[field]:
                    raise ValueError("thesis identity differs")
    if table == "macro_context":
        for field in ("schema_version", "as_of"):
            if field in payload and payload[field] != row[field]:
                raise ValueError("context identity differs")
    model = models[table]
    version = model.model_fields.get("schema_version")
    if version is not None:
        from typing import get_args

        current = get_args(version.annotation)[0]
        saved = payload.get("schema_version")
        if type(saved) is int and 0 < saved < current:
            return "stored_only"
    model.model_validate(row if table == "operation_session" else payload, extra="ignore")
    return "current"


def publication_rows(
    path: Path,
    *,
    table: str,
    filters: dict[str, object],
    after: list[str | int | float] | None = None,
    limit: int = 1,
    full: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, object]]:
    identifier, order, columns = TABLES[table]
    time_fields = {"published_at", "reviewed_at", "started_at"}
    sort = tuple(f"julianday({key})" if key in time_fields else key for key in order)
    time_column = next((key for key in order if key in time_fields), None)
    select = "*" if full else columns
    if time_column:
        select += f", julianday({time_column}) AS page_time"
    date_column = "period_end_date" if table == "portfolio_outcome" else "as_of"
    with required_read(path, connect_read_only) as connection:
        if table == "macro_context" and ("latest" in filters or "as_of" in filters):
            from datetime import date, datetime
            from zoneinfo import ZoneInfo

            from .macro import latest_macro_context_row

            cutoff = (
                date.fromisoformat(str(filters["as_of"]))
                if "as_of" in filters
                else datetime.now(ZoneInfo("Asia/Tokyo")).date()
            )
            chosen = latest_macro_context_row(connection, as_of=cutoff)
            if chosen is None:
                return [], {}
            filters = {"context_id": chosen["context_id"]}
        if table == "thesis_review" and "thesis_id" in filters:
            parent = connection.execute(
                "SELECT 1 FROM thesis WHERE thesis_id=?", (filters["thesis_id"],)
            ).fetchone()
            if parent is None:
                raise FileNotFoundError("parent thesis unavailable")
        rows = select_page(
            connection,
            table=table,
            columns=select,
            order=sort,
            equal={
                key: value
                for key, value in filters.items()
                if key
                in {
                    identifier,
                    "ticker",
                    "review_set_id",
                    "thesis_id",
                    "horizon",
                    "status",
                    "kind",
                    "session_kind",
                }
            },
            ranges=[
                (date_column, op, filters[key])
                for key, op in (("from", ">="), ("to", "<="))
                if key in filters
            ],
            after=after,
            limit=limit,
        )
        meta: dict[str, object] = {}
        if table == "macro_context":
            head = connection.execute(
                "SELECT context_id FROM macro_context_head WHERE singleton=1"
            ).fetchone()
            meta["head_context_id"] = None if head is None else head[0]
        for row in rows:
            if full:
                row["payload"] = object_payload(row["payload"])
                # Synthetic paging key must not enter the stored model.
                historical_review = False
                if table == "thesis_review":
                    parent = connection.execute(
                        "SELECT json_extract(payload, '$.schema_version') "
                        "FROM thesis WHERE thesis_id=?",
                        (row["thesis_id"],),
                    ).fetchone()
                    historical_review = (
                        parent is not None and type(parent[0]) is int and 0 < parent[0] < 4
                    )
                if historical_review:
                    if row["payload"].get("review_id") != row["review_id"]:
                        raise ValueError("review identity differs")
                    meta["validation"] = "stored_only"
                    continue
                meta["validation"] = validate_publication(
                    table, {key: value for key, value in row.items() if key != "page_time"}
                )
        return rows, meta
