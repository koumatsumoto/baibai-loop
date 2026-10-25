"""Query-only screening publication views."""

from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path

from baibai_engine.foundation.sqlite_read import read_store_if_written
from baibai_engine.machine_analysis.screening.discovery.projection import (
    candidate_rows,
    reinvestment_coordinates,
)
from baibai_engine.machine_analysis.screening.earnings_lag import interpreted_next_earnings_status
from baibai_engine.machine_analysis.screening.run_store import ScreeningRunReader
from baibai_engine.machine_analysis.screening.run_store import (
    connect_read_only as connect_run_store_read_only,
)

from .sqlite import read_rows


def screening_calibration_method_identity(root: Path) -> tuple[str, str] | None:
    """Return the current production rules and E[r] model identity, or fail closed."""

    # Local imports keep ordinary run-store reads lightweight; only the optional
    # calibration context needs the panel contract and method configuration.
    from yaml import YAMLError

    from baibai_engine.machine_analysis.company_valuation.estimates import (
        EXPECTED_RETURN_MODEL_VERSION,
    )
    from baibai_engine.machine_analysis.screening.rule_config import (
        DEFAULT_RULES_PATH,
        load_screening_rules,
    )
    from baibai_engine.machine_analysis.screening.rules_identity import (
        production_rules_contract_hash,
    )

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

    Resolves the ambiguity ``screening run`` raises when the greatest prior
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
            "ORDER BY asof_date DESC, run_clock(run_at) DESC, run_revision_id DESC LIMIT 1",
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
        run = read_store_if_written(path, lambda: reader.get_run(run_revision_id))
    elif as_of_date is not None:
        run = read_store_if_written(path, lambda: reader.latest_run_on(as_of=as_of_date))
    else:
        run = read_store_if_written(path, lambda: reader.latest_run())
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


def _run_payload(run: object) -> dict[str, object]:
    from baibai_engine.machine_analysis.screening.run_store import RunPublication

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
        "stock_analyses": list(run.stock_analyses),
    }


__all__ = [
    "candidate_rows",
    "interpreted_next_earnings_status",
    "previous_run_revision_id",
    "reinvestment_coordinates",
    "screening_calibration_method_identity",
    "screening_run_asof_dates",
    "screening_run_payload",
]
