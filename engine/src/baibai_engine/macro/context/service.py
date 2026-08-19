"""Macro-context publication and revision queries.

Revisions published under an earlier contract stay in the table as an immutable log,
so every read filters on the current ``schema_version``: a report that the current
contract cannot express is not a report the current consumers may read.
"""

from __future__ import annotations

import shlex
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.macro.reading.rules import DEFAULT_RULES_PATH as READING_RULES_PATH

from .models import (
    MACRO_CONTEXT_SCHEMA_VERSION,
    MacroContextDocument,
    ScorecardSnapshotInput,
    require_integrated_strategy,
    require_machine_checkable_monitoring,
    require_registry_agreement,
)
from .scorecard import already_met_conditions_from_stores, evaluate_scorecard_from_stores


class MacroContextConflictError(ValueError):
    pass


class MacroContextNotFoundError(ValueError):
    pass


class MacroContextService:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def publish(
        self,
        document: MacroContextDocument,
        *,
        expected_head: str | None,
    ) -> MacroContextDocument:
        # The two checks a report can only pass against the environment it is written
        # in. Both are deliberately absent from the document contract: the tree and the
        # registry move on, and a published report has to stay readable when they do.
        _require_known_reading_revisions(document)
        require_registry_agreement(document)
        require_machine_checkable_monitoring(document)
        require_integrated_strategy(document)
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                # The head means "the operative revision under the current contract", so
                # the compare-and-swap reads it through the same filter as `head_id`. A
                # head left behind by an earlier contract reads as absent, and the first
                # revision of the new contract starts a fresh chain.
                current = _current_head_id(connection)
                if current != expected_head:
                    raise MacroContextConflictError(
                        "macro context head changed: "
                        f"expected={expected_head!r}, actual={current!r}"
                    )
                if connection.execute(
                    "SELECT 1 FROM macro_context WHERE context_id = ?", (document.context_id,)
                ).fetchone():
                    raise MacroContextConflictError(
                        f"context_id already exists: {document.context_id}"
                    )
                _require_previous_scorecard_snapshot(
                    document,
                    previous_context_id=current,
                    previous_context_as_of=_context_as_of(connection, current),
                    application_db=database_path(self._db_path).resolve(),
                )
                _require_scorecard_conditions_not_already_met(document)
                connection.execute(
                    """
                    INSERT INTO macro_context (
                        context_id, schema_version, as_of, published_at, supersedes_id, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document.context_id,
                        document.schema_version,
                        document.as_of.isoformat(),
                        document.published_at.isoformat(),
                        current,
                        canonical_json(document.payload()),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO macro_context_head(singleton, context_id) VALUES (1, ?)
                    ON CONFLICT(singleton) DO UPDATE SET context_id = excluded.context_id
                    """,
                    (document.context_id,),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return document

    def head_id(self) -> str | None:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            return _current_head_id(connection)

    def get(self, context_id: str) -> MacroContextDocument:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            row = connection.execute(
                "SELECT payload FROM macro_context WHERE context_id = ? AND schema_version = ?",
                (context_id, MACRO_CONTEXT_SCHEMA_VERSION),
            ).fetchone()
        if row is None:
            raise MacroContextNotFoundError(f"unknown context_id: {context_id}")
        return MacroContextDocument.model_validate_json(str(row[0]))

    def latest_for(self, as_of: date) -> MacroContextDocument | None:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            row = connection.execute(
                """
                SELECT payload FROM macro_context
                WHERE as_of <= ? AND schema_version = ?
                ORDER BY published_at DESC, as_of DESC, context_id DESC
                LIMIT 1
                """,
                (as_of.isoformat(), MACRO_CONTEXT_SCHEMA_VERSION),
            ).fetchone()
        return None if row is None else MacroContextDocument.model_validate_json(str(row[0]))

    def get_for(self, context_id: str, *, as_of: date) -> MacroContextDocument:
        document = self.get(context_id)
        if document.as_of > as_of:
            raise MacroContextConflictError(
                f"future macro context is not eligible: {context_id} as_of={document.as_of}"
            )
        return document


def _require_known_reading_revisions(document: MacroContextDocument) -> None:
    """Reject a cited reading revision that does not exist.

    `rules_revision` is otherwise free text, so a report could claim a reading computed
    under rules that were never written — provenance that reads as verified but is not.
    Every dated revision stays in the tree, so an older report keeps validating.
    """

    directory = READING_RULES_PATH.parent
    if not directory.is_dir():
        raise MacroContextConflictError(
            f"macro reading rules directory not found: {directory} "
            "(publish from the repository root)"
        )
    known = {path.stem for path in directory.glob("*.yaml")}
    cited = {
        reading.rules_revision
        for reading in document.inputs.reading_snapshots
        if reading.status == "ok"
    }
    cited.update(
        snapshot.rules_revision
        for snapshot in document.inputs.machine_snapshots
        if isinstance(snapshot, ScorecardSnapshotInput) and snapshot.status == "ok"
    )
    unknown = sorted(cited - known)
    if unknown:
        raise MacroContextConflictError(
            "cited macro reading revision does not exist: " + ", ".join(unknown)
        )


def _require_previous_scorecard_snapshot(
    document: MacroContextDocument,
    *,
    previous_context_id: str | None,
    previous_context_as_of: date | None,
    application_db: Path,
) -> None:
    """Require every revision after the first to cite its predecessor's scorecard."""

    if (
        previous_context_id is None
        or previous_context_as_of is None
        or document.as_of <= previous_context_as_of
    ):
        return
    candidates: list[ScorecardSnapshotInput] = []
    for snapshot in document.inputs.machine_snapshots:
        if not isinstance(snapshot, ScorecardSnapshotInput):
            continue
        if (
            snapshot.context_id == previous_context_id
            and snapshot.snapshot_asof == document.as_of
            and snapshot.status == "ok"
            and Path(snapshot.context_db).resolve() == application_db
            and _is_canonical_scorecard_snapshot(snapshot)
        ):
            candidates.append(snapshot)
    if len(candidates) != 1:
        raise MacroContextConflictError(
            "a macro context revision must include exactly one successful scorecard "
            f"snapshot for previous context {previous_context_id} at asof {document.as_of}"
        )

    snapshot = candidates[0]
    if document.core[0].previous_scorecard_snapshot_id != snapshot.input_id:
        raise MacroContextConflictError(
            "the regime summary previous_scorecard_snapshot_id must reference "
            "the previous scorecard snapshot input"
        )
    rules_path = READING_RULES_PATH.parent / f"{snapshot.rules_revision}.yaml"
    recomputed = evaluate_scorecard_from_stores(
        context_db=application_db,
        indicators_db_path=Path(snapshot.indicators_db),
        context_id=previous_context_id,
        asof=document.as_of,
        accessed_at=document.published_at,
        rules_path=rules_path,
    )
    if (
        recomputed.machine_snapshot.result_digest != snapshot.result_digest
        or recomputed.machine_snapshot.input_id != snapshot.input_id
    ):
        raise MacroContextConflictError(
            "previous scorecard snapshot identity does not match a read-only recomputation"
        )


def _require_scorecard_conditions_not_already_met(document: MacroContextDocument) -> None:
    """Reject a scenario condition that was already true when the report was written.

    A condition the closing observation already meets is settled `met` by the first
    observation after `as_of` whatever happens, so it records no view. One such condition
    in a report of six to nine lifts the accumulated probability-versus-outcome tally,
    which is the only calibration path the macro layer has.

    The store to ask is the one the report itself was scored against: every revision
    after the first carries a canonical scorecard snapshot naming it, and
    `_require_previous_scorecard_snapshot` has already proved that store readable by
    recomputing the predecessor's digest from it. A report with no such snapshot is the
    first revision of a contract, which has no store to name — and inventing one here
    would make the gate read whichever store happened to sit at the canonical path.
    """

    snapshot = next(
        (
            item
            for item in document.inputs.machine_snapshots
            if isinstance(item, ScorecardSnapshotInput)
            and item.status == "ok"
            and _is_canonical_scorecard_snapshot(item)
        ),
        None,
    )
    if snapshot is None:
        return
    already_met = already_met_conditions_from_stores(
        document,
        indicators_db_path=Path(snapshot.indicators_db),
        rules_path=READING_RULES_PATH.parent / f"{snapshot.rules_revision}.yaml",
    )
    if already_met:
        detail = "; ".join(
            f"{item.case}[{item.condition_index}] {item.series_id} "
            f"{item.comparison} {item.threshold} was already true at as_of "
            f"({item.observed_at}: {item.value})"
            for item in already_met
        )
        raise MacroContextConflictError(
            f"scorecard conditions already hold at as_of {document.as_of}: {detail}"
        )


def _is_canonical_scorecard_snapshot(snapshot: ScorecardSnapshotInput) -> bool:
    indicators_db = Path(snapshot.indicators_db).resolve()
    rules_path = (READING_RULES_PATH.parent / f"{snapshot.rules_revision}.yaml").resolve()
    expected = shlex.join(
        (
            "baibai-engine",
            "macro",
            "context",
            "--db",
            str(Path(snapshot.context_db).resolve()),
            "scorecard",
            "--context-id",
            snapshot.context_id,
            "--asof",
            snapshot.snapshot_asof.isoformat(),
            "--indicators-db",
            str(indicators_db),
            "--rules",
            str(rules_path),
            "--format",
            "json",
        )
    )
    return (
        snapshot.command == expected
        and Path(snapshot.context_db).is_absolute()
        and Path(snapshot.indicators_db).is_absolute()
        and indicators_db.is_file()
        and rules_path.is_file()
    )


def _context_as_of(
    connection: sqlite3.Connection,
    context_id: str | None,
) -> date | None:
    if context_id is None:
        return None
    row = connection.execute(
        "SELECT as_of FROM macro_context WHERE context_id = ?",
        (context_id,),
    ).fetchone()
    return None if row is None else date.fromisoformat(str(row[0]))


def _current_head_id(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        """
        SELECT head.context_id FROM macro_context_head AS head
        JOIN macro_context AS context ON context.context_id = head.context_id
        WHERE head.singleton = 1 AND context.schema_version = ?
        """,
        (MACRO_CONTEXT_SCHEMA_VERSION,),
    ).fetchone()
    return None if row is None else str(row[0])


__all__ = [
    "MacroContextConflictError",
    "MacroContextNotFoundError",
    "MacroContextService",
]
