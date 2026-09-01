"""Query-only screening publication views."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.screening.run_store import ScreeningRunReader
from baibai_engine.screening.run_store import connect_read_only as connect_run_store_read_only

from .sqlite import is_unwritten_store, read_rows


def screening_calibration_method_identity(root: Path) -> tuple[str, str] | None:
    """Return the current production rules and E[r] model identity, or fail closed."""

    # Local imports keep ordinary run-store reads lightweight; only the optional
    # calibration context needs the panel contract and method configuration.
    from yaml import YAMLError

    from baibai_engine.screening.estimates import EXPECTED_RETURN_MODEL_VERSION
    from baibai_engine.screening.rule_config import DEFAULT_RULES_PATH, load_screening_rules
    from baibai_engine.screening.rules_identity import production_rules_contract_hash

    try:
        rules = load_screening_rules(root / DEFAULT_RULES_PATH)
    except (OSError, UnicodeError, ValueError, YAMLError):
        return None
    return (
        production_rules_contract_hash(rules.model_dump_json()),
        EXPECTED_RETURN_MODEL_VERSION,
    )


def previous_run_revision_id(path: Path, asof: date) -> str | None:
    """Return the newest revision of the greatest prior as-of, or None when none exists.

    Resolves the ambiguity ``screening review-set publish`` raises when the greatest prior
    as-of holds more than one revision: the store's canonical ordering
    (``asof_date``, ``run_at``, ``run_revision_id`` descending) picks one
    deterministically. This answer feeds a run that is about to be written, so
    unlike the view readers here it does not degrade: any failure to read the store
    raises rather than becoming "no previous run", which would publish a run whose
    drift comparison is silently missing. A missing file is still None — the first
    run of a fresh store legitimately has no predecessor.
    """

    if not path.is_file():
        return None
    with closing(connect_run_store_read_only(path)) as connection:
        row = connection.execute(
            "SELECT run_revision_id FROM screening_run WHERE asof_date < ? "
            "ORDER BY asof_date DESC, run_at DESC, run_revision_id DESC LIMIT 1",
            (asof.isoformat(),),
        ).fetchone()
    return None if row is None else str(row[0])


def screening_run_payload(
    path: Path,
    *,
    run_revision_id: str | None = None,
    as_of_date: date | None = None,
) -> dict[str, object] | None:
    if not path.is_file():
        return None
    reader = ScreeningRunReader(path)
    if run_revision_id is not None and as_of_date is not None:
        raise ValueError("run_revision_id and as_of_date are mutually exclusive")
    if run_revision_id is not None:
        run = _absent_as_none(path, lambda: reader.get_run(run_revision_id))
    elif as_of_date is not None:
        rows = read_rows(
            path,
            """
            SELECT run_revision_id
            FROM screening_run
            WHERE asof_date = ?
            ORDER BY run_at DESC, run_revision_id DESC
            LIMIT 1
            """,
            (as_of_date.isoformat(),),
            connector=connect_run_store_read_only,
        )
        run = _absent_as_none(path, lambda: reader.get_run(str(rows[0][0]))) if rows else None
    else:
        run = _absent_as_none(path, lambda: reader.latest_run())
    return None if run is None else _run_payload(run)


def screening_run_asof_dates(path: Path, *, limit: int = 31) -> list[date]:
    """Return retained run dates newest-first, with one entry per as-of date."""

    if limit < 1:
        raise ValueError("limit must be positive")
    rows = read_rows(
        path,
        """
        SELECT DISTINCT asof_date
        FROM screening_run
        ORDER BY asof_date DESC
        LIMIT ?
        """,
        (limit,),
        connector=connect_run_store_read_only,
    )
    return [date.fromisoformat(str(row[0])) for row in rows]


def screening_review_set_payloads(
    path: Path,
    *,
    run_revision_id: str | None = None,
) -> list[dict[str, object]]:
    """Project retained pre-cutover publications for read-only Web history.

    Point reads and every writer continue to require the current typed contract. This
    projection exists only so one retained historical root cannot stop the whole Web
    export during rollout; it neither invents missing provenance nor makes that root a
    valid input to Research Triage.
    """

    if run_revision_id is None:
        query = """
            SELECT s.review_set_id, s.run_revision_id, r.asof_date, s.created_at, s.payload
            FROM review_set AS s
            JOIN screening_run AS r USING (run_revision_id)
            ORDER BY r.asof_date DESC, s.created_at DESC, s.review_set_id DESC
        """
        parameters: tuple[object, ...] = ()
    else:
        query = """
            SELECT s.review_set_id, s.run_revision_id, r.asof_date, s.created_at, s.payload
            FROM review_set AS s
            JOIN screening_run AS r USING (run_revision_id)
            WHERE s.run_revision_id = ?
            ORDER BY r.asof_date DESC, s.created_at DESC, s.review_set_id DESC
        """
        parameters = (run_revision_id,)
    rows = read_rows(
        path,
        query,
        parameters,
        connector=connect_run_store_read_only,
    )
    projected: list[dict[str, object]] = []
    for row in rows:
        payload = json.loads(str(row["payload"]))
        _validate_read_model_review_set(payload)
        projected.append(
            {
                "review_set_id": str(row["review_set_id"]),
                "run_revision_id": str(row["run_revision_id"]),
                "as_of_date": str(row["asof_date"]),
                "created_at": str(row["created_at"]),
                "payload": payload,
            }
        )
    return projected


def _validate_read_model_review_set(payload: object) -> None:
    from baibai_engine.screening.discovery.review_set import (
        PublishedReviewSet,
        ReviewSetContractError,
        validate_review_set_shape,
    )

    if not isinstance(payload, dict):
        raise ReviewSetContractError("published review set payload is not an object")
    try:
        validate_review_set_shape(payload)
        return
    except ReviewSetContractError as error:
        contract_error = error

    current_fields = frozenset(PublishedReviewSet.model_fields)
    extra_fields = set(payload) - current_fields
    if (
        "screening_rules_hash" in payload
        or len(extra_fields) != 1
        or not isinstance(payload[next(iter(extra_fields))], Mapping)
    ):
        raise contract_error
    typed_projection = {key: payload[key] for key in current_fields if key in payload}
    typed_projection["screening_rules_hash"] = "historical-provenance-unavailable"
    try:
        PublishedReviewSet.model_validate(typed_projection)
    except ValueError:
        raise contract_error from None


def _absent_as_none[T](path: Path, read: Callable[[], T]) -> T | None:
    """Read the run store, treating a store the writer has not created as no rows.

    Same rule as ``read_rows``, applied where the read goes through the run-store
    reader rather than one statement: an absent file and an absent table both mean
    nothing has been published here, and everything else still raises.
    """

    if not path.is_file():
        return None
    try:
        return read()
    except sqlite3.OperationalError as error:
        if not is_unwritten_store(error):
            raise
        return None


def _run_payload(run: object) -> dict[str, object]:
    from baibai_engine.screening.run_store import RunPublication

    if not isinstance(run, RunPublication):  # pragma: no cover - internal contract
        raise TypeError("expected RunPublication")
    screening_rules_hash = run.payload.get("screening_rules_hash")
    er_model_version = run.payload.get("er_model_version")
    return {
        "run_revision_id": run.run_revision_id,
        "public_run_id": run.public_run_id,
        "run_date": run.run_date,
        "as_of_date": run.as_of_date,
        "run_at": run.run_at,
        "universe_size": run.universe_size,
        "rules_ref": run.rules_ref,
        "screening_rules_hash": screening_rules_hash,
        "er_model_version": er_model_version,
        "payload": run.payload,
        "security_analyses": list(run.security_analyses),
    }


__all__ = [
    "previous_run_revision_id",
    "screening_calibration_method_identity",
    "screening_review_set_payloads",
    "screening_run_asof_dates",
    "screening_run_payload",
]
