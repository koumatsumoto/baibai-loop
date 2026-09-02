"""Build a v20 application store by removing the retired AI-only Thesis field.

This one-shot operator tool serves the Research to Thesis publication step by 産む a
current application store and 止める before recorded identities or other canonical
application rows can be lost.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.schema import APPLICATION_SCHEMA_VERSION, SCHEMA_SQL
from baibai_engine.research.thesis import (
    DerivedNamespace,
    EstimatesNamespace,
    EvidenceOverride,
    InputSnapshot,
    JudgmentNamespace,
    PermanentLossRisk,
    ThesisDocument,
)

SOURCE_VERSION = 19
TARGET_VERSION = 20

_UNCHANGED_TABLES: dict[str, tuple[str, ...]] = {
    "task": (
        "task_id",
        "status",
        "kind",
        "ticker",
        "due_date",
        "event_date",
        "created_at",
        "closed_at",
        "payload",
    ),
    "macro_context": (
        "context_id",
        "schema_version",
        "as_of",
        "published_at",
        "supersedes_id",
        "payload",
    ),
    "macro_context_head": ("singleton", "context_id"),
    "research_triage": (
        "research_triage_id",
        "review_set_id",
        "run_revision_id",
        "as_of",
        "published_at",
        "payload",
    ),
    "thesis_review": ("review_id", "thesis_id", "reviewed_at", "payload"),
    "position_review": (
        "position_review_id",
        "ticker",
        "as_of",
        "thesis_id",
        "candidate_thesis_id",
        "payload",
    ),
    "capital_allocation_assessment": (
        "capital_allocation_assessment_id",
        "as_of",
        "published_at",
        "result",
        "research_triage_id",
        "payload",
    ),
    "operation_session": (
        "operation_id",
        "session_kind",
        "status",
        "as_of",
        "ticker",
        "started_at",
        "completed_at",
        "payload",
    ),
    "ledger_event": (
        "append_seq",
        "event_id",
        "occurred_at",
        "same_instant_order",
        "event_type",
        "ticker",
        "decision_reference",
        "payload",
    ),
    "ledger_market_price": (
        "ticker",
        "observed_at",
        "price_yen",
        "source_kind",
        "price_basis",
        "source_ref",
        "payload",
    ),
    "ledger_meta": (
        "singleton",
        "schema_version",
        "portfolio_scope",
        "as_of",
        "estimated_exit_tax_rate_bps",
        "estimated_exit_tax_basis",
        "payload",
    ),
    "portfolio_outcome": (
        "outcome_id",
        "horizon",
        "period_start_date",
        "period_end_date",
        "status",
        "payload",
    ),
}


class CutoverError(RuntimeError):
    """The source cannot be converted without guessing or losing canonical data."""


class _LegacyAIValueCaptureJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    assessment_status: Literal["material", "not_material", "unknown"]
    roles: tuple[Literal["enabler", "infrastructure", "complement", "adopter", "disrupted"], ...]
    competitive_advantage: Literal["favorable", "neutral", "adverse", "unknown"]
    pricing_power: Literal["favorable", "neutral", "adverse", "unknown"]
    capex_burden: Literal["favorable", "neutral", "adverse", "unknown"]
    customer_bargaining_power: Literal["favorable", "neutral", "adverse", "unknown"]
    value_capture_conclusion: Literal["captured", "uncertain", "not_captured", "adverse"]
    decision_weight: Literal["none", "supporting", "material"]
    rationale: Annotated[str, Field(min_length=1)]
    source_ids: Annotated[tuple[Annotated[str, Field(min_length=1)], ...], Field(min_length=1)]

    @field_validator("roles", "source_ids", mode="before")
    @classmethod
    def _parse_sequences(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _valid_decision_weight(self) -> Self:
        if len(set(self.roles)) != len(self.roles) or self.roles != tuple(sorted(self.roles)):
            raise ValueError("ai_value_capture.roles must be unique and sorted")
        if self.assessment_status == "material" and not self.roles:
            raise ValueError("material AI value-capture assessment requires at least one role")
        if self.assessment_status == "not_material" and (
            self.roles or self.decision_weight != "none"
        ):
            raise ValueError(
                "not_material AI value-capture assessment requires no roles and none weight"
            )
        if (
            self.assessment_status == "unknown"
            or self.value_capture_conclusion in {"uncertain", "not_captured", "adverse"}
        ) and self.decision_weight != "none":
            raise ValueError("uncertain or uncaptured AI value cannot carry decision weight")
        if self.decision_weight != "none" and (
            self.assessment_status != "material" or self.value_capture_conclusion != "captured"
        ):
            raise ValueError("AI decision weight requires material captured value")
        return self


class _LegacyJudgmentNamespace(JudgmentNamespace):
    ai_value_capture: _LegacyAIValueCaptureJudgment


class _LegacyThesisV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2]
    input_snapshot: InputSnapshot
    derived: DerivedNamespace
    estimates: EstimatesNamespace
    permanent_loss_risks: tuple[PermanentLossRisk, ...]
    judgment: _LegacyJudgmentNamespace
    independent_review_ref: Annotated[str, Field(min_length=1)] | None = None
    human_evidence_override: EvidenceOverride | None = None

    @field_validator("permanent_loss_risks", mode="before")
    @classmethod
    def _parse_risks(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


def _object(raw: object, *, label: str) -> dict[str, object]:
    payload = json.loads(str(raw))
    if not isinstance(payload, dict):
        raise CutoverError(f"{label} payload must be an object")
    return payload


def _transform_thesis(raw: object, *, thesis_id: str) -> str:
    payload = _object(raw, label=f"thesis {thesis_id}")
    try:
        _LegacyThesisV2.model_validate(payload)
    except ValueError as error:
        raise CutoverError(f"thesis {thesis_id} is not a current v2 payload: {error}") from error
    payload["schema_version"] = 3
    judgment = payload["judgment"]
    if not isinstance(judgment, dict) or judgment.pop("ai_value_capture", None) is None:
        raise CutoverError(f"thesis {thesis_id} has no ai_value_capture field")
    try:
        ThesisDocument.model_validate(payload)
    except ValueError as error:
        raise CutoverError(f"thesis {thesis_id} cannot become v3: {error}") from error
    return canonical_json(payload)


def _read_source(source: Path) -> tuple[list[tuple[object, ...]], dict[str, int]]:
    uri = f"{source.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SOURCE_VERSION:
            raise CutoverError(f"source user_version is {version}; expected {SOURCE_VERSION}")
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise CutoverError("source integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise CutoverError("source foreign_key_check failed")
        rows = connection.execute(
            "SELECT thesis_id, ticker, as_of, recommendation, published_at, supersedes_id, "
            "payload, core_sha256 FROM thesis ORDER BY thesis_id"
        ).fetchall()
        transformed: list[tuple[object, ...]] = []
        for row in rows:
            thesis_id = str(row[0])
            if not isinstance(row[7], str) or len(row[7]) != 64:
                raise CutoverError(f"thesis {thesis_id} has no recorded core_sha256")
            transformed.append((*row[:6], _transform_thesis(row[6], thesis_id=thesis_id), row[7]))
        counts = {
            table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # nosec B608
            for table in (*_UNCHANGED_TABLES, "thesis")
        }
    return transformed, counts


def _copy_rows(destination: sqlite3.Connection, table: str, columns: tuple[str, ...]) -> None:
    names = ", ".join(columns)
    destination.execute(
        f"INSERT INTO {table} ({names}) SELECT {names} FROM source.{table}"  # nosec B608
    )


def _require_unchanged(
    destination: sqlite3.Connection, table: str, columns: tuple[str, ...]
) -> None:
    names = ", ".join(columns)
    for left, right in ((table, f"source.{table}"), (f"source.{table}", table)):
        changed = destination.execute(
            f"SELECT {names} FROM {left} EXCEPT SELECT {names} FROM {right}"  # nosec B608
        ).fetchone()
        if changed is not None:
            raise CutoverError(f"non-Thesis table changed: {table}")


def cutover(source: Path, output: Path) -> dict[str, object]:
    """Create ``output`` from one verified v19 backup and report proved changes."""
    if APPLICATION_SCHEMA_VERSION != TARGET_VERSION:
        raise CutoverError(
            f"runtime application schema is {APPLICATION_SCHEMA_VERSION}; expected {TARGET_VERSION}"
        )
    if output.exists():
        raise CutoverError(f"output already exists: {output}")
    if not source.is_file():
        raise CutoverError(f"source does not exist: {source}")
    thesis_rows, source_counts = _read_source(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(f"{output.resolve().as_uri()}?mode=rwc", uri=True)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("ATTACH DATABASE ? AS source", (f"{source.resolve().as_uri()}?mode=ro",))
        connection.executescript(SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("PRAGMA defer_foreign_keys = ON")
        for table, columns in _UNCHANGED_TABLES.items():
            _copy_rows(connection, table, columns)
        connection.executemany("INSERT INTO thesis VALUES (?, ?, ?, ?, ?, ?, ?, ?)", thesis_rows)
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise CutoverError(f"foreign_key_check failed: {violations!r}")
        for table, columns in _UNCHANGED_TABLES.items():
            _require_unchanged(connection, table, columns)
        connection.execute(f"PRAGMA user_version = {TARGET_VERSION}")
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise CutoverError("output integrity_check failed")
        output_counts = {
            table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # nosec B608
            for table in (*_UNCHANGED_TABLES, "thesis")
        }
        if output_counts != source_counts:
            raise CutoverError(f"row counts changed: {source_counts!r} -> {output_counts!r}")
        return {
            "schema_version": TARGET_VERSION,
            "rows": output_counts,
            "transformed_theses": len(thesis_rows),
        }
    except BaseException:
        connection.close()
        output.unlink(missing_ok=True)
        raise
    finally:
        if output.exists():
            connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(cutover(args.source, args.output), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
