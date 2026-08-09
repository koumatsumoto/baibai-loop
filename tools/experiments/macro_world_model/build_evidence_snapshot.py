"""Build a deterministic Macro World Model evidence snapshot.

The command obtains the public ``macro reading --format json`` view and joins it to
the L1 observation vintages.  Economic interpretation stays out of this artifact;
selection decisions and their reasons are carried only as coverage metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from contextlib import redirect_stdout
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TextIO, cast

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.macro.indicators import cli as macro_cli

SCHEMA_VERSION = 1
MATERIALITY_POLICY_REVISION = "stage-a-v1"


class EvidenceSnapshotError(ValueError):
    """Raised when inputs cannot produce an auditable deterministic snapshot."""


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EvidenceSnapshotError(f"{label} must be a mapping")
    return cast(Mapping[str, object], value)


def _mapping_list(value: object, *, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise EvidenceSnapshotError(f"{label} must be a list of mappings")
    return [cast(Mapping[str, object], item) for item in value]


def _finite_number(value: object) -> float | None:
    if not isinstance(value, int | float):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_payload_sha256(payload: Mapping[str, object]) -> str:
    """Hash canonical content while excluding generation time from identity."""

    identity = {
        key: value
        for key, value in payload.items()
        if key not in {"created_at", "canonical_payload_sha256"}
    }
    return hashlib.sha256(_canonical_json(identity).encode()).hexdigest()


def _read_reading(*, as_of: date, macro_db: Path) -> Mapping[str, object]:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = macro_cli.main(
            [
                "reading",
                "--asof",
                as_of.isoformat(),
                "--db",
                str(macro_db),
                "--format",
                "json",
            ]
        )
    if exit_code != 0:
        raise EvidenceSnapshotError(f"macro reading failed with exit code {exit_code}")
    parsed = json.loads(stdout.getvalue())
    return _mapping(parsed, label="macro reading output")


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise EvidenceSnapshotError(f"macro store does not exist: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _effective_observations(
    connection: sqlite3.Connection, *, series_id: str, as_of: date
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            WITH ranked AS (
              SELECT observations.*,
                     ROW_NUMBER() OVER (
                       PARTITION BY observed_at ORDER BY vintage_at DESC
                     ) AS vintage_rank
              FROM observations
              WHERE series_id = ?
                AND observed_at <= ?
                AND fetch_status IN ('ok', 'retracted')
            )
            SELECT observed_at, value, unit, vintage_at, fetch_status, source_url
            FROM ranked
            WHERE vintage_rank = 1 AND fetch_status = 'ok'
            ORDER BY observed_at DESC
            LIMIT 2
            """,
            (series_id, as_of.isoformat()),
        )
    )


def _revision_rows(
    connection: sqlite3.Connection, *, series_id: str, as_of: date
) -> list[dict[str, object]]:
    revised_dates = connection.execute(
        """
        SELECT observed_at
        FROM observations
        WHERE series_id = ?
          AND observed_at <= ?
          AND fetch_status IN ('ok', 'retracted')
        GROUP BY observed_at
        HAVING COUNT(*) > 1
        ORDER BY observed_at
        """,
        (series_id, as_of.isoformat()),
    )
    revisions: list[dict[str, object]] = []
    for date_row in revised_dates:
        observed_at = str(date_row["observed_at"])
        vintages = list(
            connection.execute(
                """
                SELECT value, unit, vintage_at, fetch_status, source_url
                FROM observations
                WHERE series_id = ?
                  AND observed_at = ?
                  AND fetch_status IN ('ok', 'retracted')
                ORDER BY vintage_at
                """,
                (series_id, observed_at),
            )
        )
        revisions.append(
            {
                "observed_at": observed_at,
                "vintages": [
                    {
                        "value": float(row["value"]),
                        "unit": str(row["unit"]),
                        "vintage_at": str(row["vintage_at"]),
                        "fetch_status": str(row["fetch_status"]),
                        "source_url": str(row["source_url"]),
                    }
                    for row in vintages
                ],
            }
        )
    return revisions


def _release_change(
    reading: Mapping[str, object], effective: Sequence[sqlite3.Row]
) -> dict[str, object] | None:
    current_value = _finite_number(reading.get("latest_value"))
    current_date = reading.get("observed_at")
    if current_value is None or not isinstance(current_date, str):
        return None
    prior = next((row for row in effective if str(row["observed_at"]) < current_date), None)
    if prior is None:
        return None
    prior_value = float(prior["value"])
    absolute = current_value - prior_value
    percent = None if prior_value == 0 else absolute / abs(prior_value) * 100
    return {
        "from_observed_at": str(prior["observed_at"]),
        "to_observed_at": current_date,
        "from_value": prior_value,
        "to_value": current_value,
        "absolute_change": absolute,
        "percent_change": percent,
        "unit": reading.get("unit"),
    }


def _machine_reasons(
    reading: Mapping[str, object],
    *,
    release_change: Mapping[str, object] | None,
    revisions: Sequence[Mapping[str, object]],
) -> list[str]:
    reasons: list[str] = []
    if reading.get("stale") is True:
        reasons.append("stale")
    if reading.get("insufficient_history") is True:
        reasons.append("insufficient_history")
    flags = reading.get("flags")
    if isinstance(flags, list) and flags:
        reasons.append("flags")
    z_score = _finite_number(reading.get("z_score"))
    if z_score is not None and abs(z_score) >= 3:
        reasons.append("extreme_z_score")
    percentile = _finite_number(reading.get("percentile"))
    if percentile is not None and (percentile <= 0.05 or percentile >= 0.95):
        reasons.append("extreme_percentile")
    short = reading.get("short_trend")
    long = reading.get("long_trend")
    if isinstance(short, Mapping) and isinstance(long, Mapping):
        short_direction = short.get("direction")
        long_direction = long.get("direction")
        if (
            isinstance(short_direction, str)
            and isinstance(long_direction, str)
            and short_direction != long_direction
        ):
            reasons.append("trend_direction_difference")
    if release_change is not None and release_change.get("absolute_change") != 0:
        reasons.append("release_change")
    if revisions:
        reasons.append("revision")
    return reasons


def _coverage_config(
    value: Mapping[str, object] | None,
) -> tuple[
    dict[str, Mapping[str, object]],
    dict[str, Mapping[str, object]],
    dict[str, Mapping[str, object]],
]:
    config = value or {}
    standing_rows = _mapping_list(config.get("standing_coverage", []), label="standing_coverage")
    analyst_rows = _mapping_list(config.get("analyst_additions", []), label="analyst_additions")
    decision_rows = _mapping_list(config.get("decisions", []), label="decisions")

    def index(
        rows: Sequence[Mapping[str, object]], *, label: str
    ) -> dict[str, Mapping[str, object]]:
        result: dict[str, Mapping[str, object]] = {}
        for row in rows:
            series_id = row.get("series_id")
            if not isinstance(series_id, str) or not series_id:
                raise EvidenceSnapshotError(f"{label} entries require series_id")
            if series_id in result:
                raise EvidenceSnapshotError(f"duplicate {label} series_id: {series_id}")
            result[series_id] = row
        return result

    return (
        index(standing_rows, label="standing_coverage"),
        index(analyst_rows, label="analyst_additions"),
        index(decision_rows, label="decisions"),
    )


def build_snapshot(
    *,
    reading: Mapping[str, object],
    macro_db: Path,
    coverage_config: Mapping[str, object] | None = None,
    created_at: datetime | None = None,
) -> dict[str, object]:
    """Join one reading snapshot to L1 vintages and a coverage manifest."""

    as_of_raw = reading.get("asof")
    if not isinstance(as_of_raw, str):
        raise EvidenceSnapshotError("macro reading output requires asof")
    as_of = date.fromisoformat(as_of_raw)
    series_rows = _mapping_list(reading.get("series"), label="macro reading series")
    by_id: dict[str, Mapping[str, object]] = {}
    for row in series_rows:
        series_id = row.get("series_id")
        if not isinstance(series_id, str) or not series_id:
            raise EvidenceSnapshotError("every reading row requires series_id")
        if series_id in by_id:
            raise EvidenceSnapshotError(f"duplicate reading series_id: {series_id}")
        by_id[series_id] = row

    standing, additions, decisions = _coverage_config(coverage_config)
    unknown = (set(standing) | set(additions) | set(decisions)) - set(by_id)
    if unknown:
        raise EvidenceSnapshotError(f"coverage config references unknown series: {sorted(unknown)}")

    scan: list[dict[str, object]] = []
    machine_candidates: dict[str, list[str]] = {}
    with _open_read_only(macro_db) as connection:
        for series_id in sorted(by_id):
            row = by_id[series_id]
            effective = _effective_observations(connection, series_id=series_id, as_of=as_of)
            revisions = _revision_rows(connection, series_id=series_id, as_of=as_of)
            change = _release_change(row, effective)
            reasons = _machine_reasons(row, release_change=change, revisions=revisions)
            if reasons:
                machine_candidates[series_id] = reasons
            scan.append(
                {
                    "evidence_id": f"series:{series_id}",
                    "series_id": series_id,
                    "name": row.get("name"),
                    "category": row.get("category"),
                    "geography": row.get("geography"),
                    "frequency": row.get("frequency"),
                    "unit": row.get("unit"),
                    "latest_value": row.get("latest_value"),
                    "observed_at": row.get("observed_at"),
                    "stale": row.get("stale"),
                    "insufficient_history": row.get("insufficient_history"),
                    "flags": row.get("flags"),
                    "z_score": row.get("z_score"),
                    "percentile": row.get("percentile"),
                    "short_trend": row.get("short_trend"),
                    "long_trend": row.get("long_trend"),
                    "release_change": change,
                    "revisions": revisions,
                    "machine_materiality_reasons": reasons,
                }
            )

    candidate_ids = set(machine_candidates) | set(standing) | set(additions)
    non_candidates = set(decisions) - candidate_ids
    if non_candidates:
        raise EvidenceSnapshotError(
            f"coverage decisions reference non-candidates: {sorted(non_candidates)}"
        )
    manifest: list[dict[str, object]] = []
    for series_id in sorted(candidate_ids):
        decision = decisions.get(series_id, {})
        status = decision.get("status", "selected")
        if status not in {"selected", "excluded"}:
            raise EvidenceSnapshotError(f"invalid decision status for {series_id}: {status}")
        reason = decision.get("reason")
        if status == "excluded" and (not isinstance(reason, str) or not reason.strip()):
            raise EvidenceSnapshotError(f"excluded candidate requires a reason: {series_id}")
        manifest.append(
            {
                "candidate_id": f"series:{series_id}",
                "series_id": series_id,
                "candidate_sources": sorted(
                    [
                        source
                        for source, present in (
                            ("machine_rules", series_id in machine_candidates),
                            ("standing_coverage", series_id in standing),
                            ("analyst_addition", series_id in additions),
                        )
                        if present
                    ]
                ),
                "machine_reasons": machine_candidates.get(series_id, []),
                "standing_block": standing.get(series_id, {}).get("block"),
                "analyst_reason": additions.get(series_id, {}).get("reason"),
                "status": status,
                "exclusion_reason": reason if status == "excluded" else None,
            }
        )

    timestamp = created_at or datetime.now(UTC)
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "macro-world-model-evidence-snapshot",
        "as_of": as_of.isoformat(),
        "created_at": timestamp.isoformat(),
        "reading_rules_revision": reading.get("rules_revision"),
        "materiality_policy_revision": MATERIALITY_POLICY_REVISION,
        "coverage_scan": scan,
        "coverage_manifest": manifest,
    }
    payload["canonical_payload_sha256"] = canonical_payload_sha256(payload)
    return payload


def _load_yaml_mapping(path: Path) -> Mapping[str, object]:
    return _mapping(safe_load(path.read_text(encoding="utf-8")), label=str(path))


def _write_new(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError as error:
        raise EvidenceSnapshotError(f"refusing to overwrite existing output: {path}") from error


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", required=True, type=date.fromisoformat)
    parser.add_argument("--macro-db", type=Path, default=Path("stores/macro/macro.sqlite"))
    parser.add_argument("--coverage-config", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _parse_args(argv)
    output = stdout or sys.stdout
    try:
        reading = _read_reading(as_of=args.asof, macro_db=args.macro_db)
        coverage = (
            _load_yaml_mapping(args.coverage_config) if args.coverage_config is not None else None
        )
        snapshot = build_snapshot(
            reading=reading,
            macro_db=args.macro_db,
            coverage_config=coverage,
        )
        _write_new(args.output, snapshot)
    except (EvidenceSnapshotError, OSError, sqlite3.Error, ValueError) as error:
        print(f"error: {error}", file=output)
        return 1
    print(
        _canonical_json(
            {
                "output": str(args.output),
                "canonical_payload_sha256": snapshot["canonical_payload_sha256"],
                "series": len(cast(list[object], snapshot["coverage_scan"])),
            }
        ),
        file=output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
