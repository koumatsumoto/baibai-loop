"""Rebuild every retained master snapshot from one fixed native selection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from pydantic import JsonValue

from baibai_engine.data_collection.providers.jquants.contracts import RetainedRequest
from baibai_engine.data_collection.providers.jquants.master_native import (
    FixedMasterL1,
    MasterL1,
    read_master_requests,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.master_inputs import (
    INPUT_FORMAT,
    read_master_input,
    retain_master_input,
)
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.prices.calendar_context import (
    confirmed_closed_master_bases,
)
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import (
    MASTER_SOURCE,
    MASTER_VALIDATION_ERROR_PREFIX,
    master_coverage_key,
    validate_master_snapshot,
)
from baibai_engine.machine_analysis.observations.stocks.storage_contracts import (
    JQUANTS_MASTER_SNAPSHOTS_DDL,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, ObjectStore
from baibai_engine.storage.sqlite.source_cache.coverage import (
    covered_intervals,
    record_source_coverage,
)
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only


@contextmanager
def _projection() -> Iterator[sqlite3.Connection]:
    # The history may exceed memory; only one native pack and one snapshot's rows
    # need to be decoded at a time. This disposable DB is never a future input.
    with (
        tempfile.TemporaryDirectory(prefix=".master-projection-") as directory,
        closing(sqlite3.connect(Path(directory) / "master.sqlite")) as connection,
    ):
        yield connection


def _days(start: date, end: date) -> tuple[date, ...]:
    if start > end:
        raise ValueError("invalid master rebuild interval")
    return tuple(start + timedelta(days=i) for i in range((end - start).days + 1))


def _validate_clock(scopes: dict[str, Any], evaluated_at: datetime) -> None:
    if evaluated_at.utcoffset() is None:
        raise ValueError("evaluation time requires a timezone")
    for scope in scopes.values():
        for field in ("confirmed_at", "last_attempt_at", "changed_at"):
            if (stamp := scope.get(field)) and datetime.fromisoformat(stamp) > evaluated_at:
                raise ValueError("master confirmation state is later than the evaluation clock")


def _project(
    projection: sqlite3.Connection,
    requests: Iterable[RetainedRequest],
    *,
    scopes: dict[str, Any],
    evaluated_at: datetime | None = None,
    required_snapshot: str | None = None,
) -> dict[str, Any]:
    """Validate each adopted whole snapshot before exposing any replacement rows."""
    projection.executescript(JQUANTS_MASTER_SNAPSHOTS_DDL)
    projection.execute(
        "CREATE TABLE master_receipt_status("
        "day TEXT PRIMARY KEY, request_id TEXT, status TEXT NOT NULL, row_count INTEGER NOT NULL,"
        "failed INTEGER NOT NULL, held INTEGER NOT NULL, confirmed_at TEXT, last_attempt_at TEXT,"
        "error TEXT)"
    )
    adopted = {scope["adopted"]: day for day, scope in scopes.items() if scope.get("adopted")}
    counts: dict[str, int] = {}
    invalid: dict[str, str] = {}
    for item in requests:
        if (
            evaluated_at is not None
            and item.last_received_at is not None
            and item.last_received_at > evaluated_at
        ):
            raise ValueError("master input contains receipts later than the evaluation clock")
        day = adopted.get(item.request_id)
        if day is None:
            continue  # Failure and held receipts never replace the retained adopted snapshot.
        if item.request is None or item.error is not None:
            raise ValueError("adopted master request is incomplete")
        if (
            item.request.source != "master"
            or set(item.request.params) != {"date"}
            or date.fromisoformat(item.request.params["date"]) != date.fromisoformat(day)
        ):
            raise ValueError("adopted master request differs from its fixed date")
        try:
            snapshot = validate_master_snapshot(item.request.records(), date.fromisoformat(day))
        except JQuantsProviderError as error:
            # An old bad receipt must not poison all later current-date acquisitions.
            # It remains an explicit unavailable boundary, never a valid empty snapshot.
            closed = scopes[day].get("closed_basis")
            if day == required_snapshot and not closed:
                raise
            prefix = "closed master request: " if closed else MASTER_VALIDATION_ERROR_PREFIX
            invalid[day] = prefix + str(error)
            continue
        projection.executemany("INSERT INTO jquants_master_snapshots VALUES (?,?,?)", snapshot.rows)
        counts[day] = snapshot.persisted_count
    if set(counts) | set(invalid) != set(adopted.values()):
        raise ValueError("native master selection lacks an adopted request")
    selected_snapshot = required_snapshot
    if required_snapshot in invalid and scopes[required_snapshot].get("closed_basis"):
        # A verified closed request is not a new usable universe. Preserve a
        # valid holiday snapshot on later business days, but skip closed failures.
        selected_snapshot = max(
            (
                day
                for day in adopted.values()
                if day <= required_snapshot
                and not (day in invalid and scopes[day].get("closed_basis"))
            ),
            default=None,
        )
        if selected_snapshot in invalid:
            raise JQuantsProviderError(invalid[selected_snapshot])
    invalid_dates = sorted(day for day in invalid if not scopes[day].get("closed_basis"))
    summary: dict[str, Any] = {
        "snapshot_days": len(scopes),
        "adopted_requests": len(adopted),
        "rows": sum(counts.values()),
        "selected_snapshot_date": selected_snapshot,
        "unavailable_days": 0,
        "partial_days": 0,
        "failed_days": 0,
        "held_days": 0,
        "invalid_days": len(invalid_dates),
        "invalid_snapshot_dates": invalid_dates[:12],
        "additional_invalid_snapshot_dates": max(0, len(invalid_dates) - 12),
        "closed_snapshot_days": sum(bool(scope.get("closed_basis")) for scope in scopes.values()),
    }
    for day, scope in sorted(scopes.items()):
        failed, held = bool(scope.get("failure")), bool(scope.get("held"))
        status = "unavailable" if day not in counts else "partial" if failed or held else "ok"
        projection.execute(
            "INSERT INTO master_receipt_status VALUES (?,?,?,?,?,?,?,?,?)",
            (
                day,
                scope.get("adopted"),
                status,
                counts.get(day, 0),
                int(failed),
                int(held),
                scope.get("confirmed_at"),
                scope.get("last_attempt_at"),
                invalid.get(day),
            ),
        )
        summary["unavailable_days"] += int(status == "unavailable")
        summary["partial_days"] += int(status == "partial")
        summary["failed_days"] += int(failed)
        summary["held_days"] += int(held)
    summary["coverage_status"] = (
        "partial" if summary["unavailable_days"] or summary["partial_days"] else "complete"
    )
    return summary


def fixed_input(
    store: ObjectStore,
    fixed: FixedMasterL1,
    *,
    as_of: date,
    evaluated_at: datetime,
    days: Sequence[date] | None = None,
    closed_bases: Mapping[date, date] | None = None,
) -> InputSnapshot:
    """Retain acquisition membership and state without decoding any source pack."""
    head = fixed.value()
    scopes = head["scopes"]
    if days is not None:
        if any(day.isoformat() not in scopes for day in days):
            raise ValueError("native master has no retained requested snapshot date")
        scopes = {day.isoformat(): scopes[day.isoformat()] for day in days}
    if not scopes:
        raise ValueError("native master L1 has no retained snapshot dates")
    closed_bases = closed_bases or {}
    scopes = {
        day: {**scope, "closed_basis": closed_bases[date.fromisoformat(day)].isoformat()}
        if date.fromisoformat(day) in closed_bases
        else dict(scope)
        for day, scope in scopes.items()
    }
    _validate_clock(scopes, evaluated_at)
    basis = closed_bases.get(as_of, as_of)
    retained_days = sorted(scopes)
    identities = {
        identity
        for scope in scopes.values()
        for field in ("adopted", "failure", "held")
        if (identity := scope.get(field))
    }
    adopted = [day for day, scope in scopes.items() if scope.get("adopted")]
    current = max((day for day in adopted if day <= basis.isoformat()), default=None)
    summary: dict[str, Any] = {
        "selection_start": retained_days[0],
        "selection_end": retained_days[-1],
        "as_of": as_of.isoformat(),
        "evaluated_at": evaluated_at.isoformat(),
        "master_basis_date": basis.isoformat(),
        "snapshot_days": len(scopes),
        "adopted_requests": len(adopted),
        "snapshot_date": current,
        "snapshot_status": "unavailable"
        if current is None
        else "exact_date"
        if current == as_of.isoformat()
        else "prior_snapshot",
        "unavailable_days": sum(not scope.get("adopted") for scope in scopes.values()),
        "partial_days": sum(
            bool(scope.get("adopted") and (scope.get("failure") or scope.get("held")))
            for scope in scopes.values()
        ),
        "failed_days": sum(bool(scope.get("failure")) for scope in scopes.values()),
        "held_days": sum(bool(scope.get("held")) for scope in scopes.values()),
    }
    summary["coverage_status"] = (
        "partial" if summary["unavailable_days"] or summary["partial_days"] else "complete"
    )
    for name, field, operation in (
        ("oldest_confirmed_at", "confirmed_at", min),
        ("latest_confirmed_at", "confirmed_at", max),
        ("latest_changed_at", "changed_at", max),
    ):
        stamps = [scope[field] for scope in scopes.values() if scope.get(field)]
        summary[name] = operation(stamps) if stamps else None
    descriptor = retain_master_input(
        store,
        {
            "head_sha256": fixed.content_id,
            "as_of": as_of.isoformat(),
            "scopes": scopes,
            "receipts": {identity: head["receipts"][identity] for identity in identities},
            "summary": summary,
        },
    )
    digest = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
    return InputSnapshot(
        kind="native_l1", reference="jquants-master-" + digest, sha256=digest, descriptor=descriptor
    )


def model_input(
    store: ObjectStore,
    fixed: FixedMasterL1,
    *,
    as_of: date,
    evaluated_at: datetime,
    market: Path,
) -> InputSnapshot:
    """Replace disposable master history, preserving all native records and sparse dates."""
    with closing(connect_read_only(market)) as connection:
        connection.execute("BEGIN")
        closed_bases = confirmed_closed_master_bases(
            {
                date.fromisoformat(str(day)): str(division)
                for day, division in connection.execute(
                    "SELECT day,holiday_division FROM jquants_market_calendar"
                )
            },
            coverage=covered_intervals(connection, "jquants_market_calendar"),
        )
    saved = fixed_input(
        store, fixed, as_of=as_of, evaluated_at=evaluated_at, closed_bases=closed_bases
    )
    days = tuple(date.fromisoformat(day) for day in sorted(fixed.value()["scopes"]))
    result = materialize_saved_input(store, saved, market=market, days=days, replace_all=True)
    descriptor = dict(_descriptor(saved))
    summary = descriptor["summary"]
    assert isinstance(summary, dict)
    selected = result.pop("selected_snapshot_date")
    descriptor["summary"] = {
        **summary,
        **result,
        "snapshot_date": selected,
        "snapshot_status": "unavailable"
        if selected is None
        else "exact_date"
        if selected == as_of.isoformat()
        else "prior_snapshot",
    }
    digest = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
    return InputSnapshot(
        kind="native_l1", reference="jquants-master-" + digest, sha256=digest, descriptor=descriptor
    )


def materialize_saved_input(
    store: ObjectStore,
    saved: InputSnapshot,
    *,
    market: Path,
    days: Sequence[date],
    replace_all: bool = False,
) -> dict[str, Any]:
    """Project fixed requested snapshots for preflight or the whole normal generation."""
    saved_interval(saved)
    selected = read_master_input(store, _descriptor(saved), days=days)
    scopes = selected["scopes"]
    if any(day.isoformat() not in scopes for day in days):
        raise ValueError("native master has no retained requested snapshot date")
    summary = _descriptor(saved)["summary"]
    assert isinstance(summary, dict)
    clock = summary["evaluated_at"]
    if not isinstance(clock, str):
        raise ValueError("native master selection requires its evaluation clock")
    evaluated_at = datetime.fromisoformat(clock)
    _validate_clock(scopes, evaluated_at)
    with _projection() as projection:
        result = _project(
            projection,
            read_master_requests(store, scopes=scopes, receipts=selected["receipts"], days=days),
            scopes=scopes,
            evaluated_at=evaluated_at,
            required_snapshot=_required_snapshot(saved),
        )
        # All selected snapshots must validate before any old execution row changes.
        with sqlite3.connect(market) as connection:
            if replace_all:
                connection.execute("DELETE FROM jquants_master_snapshots")
                connection.execute("DELETE FROM jquants_source_pages WHERE source='master'")
                connection.execute("DELETE FROM source_coverage WHERE source=?", (MASTER_SOURCE,))
            else:
                for day in scopes:
                    connection.execute(
                        "DELETE FROM jquants_master_snapshots WHERE snapshot_date=?", (day,)
                    )
                    connection.execute(
                        "DELETE FROM source_coverage WHERE source=? "
                        "AND coverage_start=? AND coverage_end=?",
                        (MASTER_SOURCE, day, day),
                    )
            rows = projection.execute(
                "SELECT * FROM jquants_master_snapshots ORDER BY snapshot_date,security_code"
            )
            while batch := rows.fetchmany(10000):
                connection.executemany("INSERT INTO jquants_master_snapshots VALUES (?,?,?)", batch)
            for (
                day,
                _,
                status,
                count,
                failed,
                held,
                confirmed,
                attempted,
                error,
            ) in projection.execute("SELECT * FROM master_receipt_status ORDER BY day"):
                record_source_coverage(
                    connection,
                    source=MASTER_SOURCE,
                    coverage_key=master_coverage_key(date.fromisoformat(day)),
                    coverage_start=day,
                    coverage_end=day,
                    record_count=count,
                    status=status,
                    fetched_at_utc=confirmed or attempted or evaluated_at.isoformat(),
                    error=error or ("native acquisition unresolved" if failed or held else None),
                )
    return result


def _descriptor(saved: InputSnapshot) -> dict[str, JsonValue]:
    saved = InputSnapshot.model_validate_json(saved.model_dump_json())
    if (
        saved.kind != "native_l1"
        or saved.descriptor is None
        or saved.descriptor.get("format") != INPUT_FORMAT
    ):
        raise ValueError("a digest-verified native master input is required")
    return saved.descriptor


def saved_interval(saved: InputSnapshot) -> tuple[date, date]:
    summary = _descriptor(saved)["summary"]
    if not isinstance(summary, dict):
        raise ValueError("native master input requires a selection summary")
    start, end = summary.get("selection_start"), summary.get("selection_end")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("native master input requires its selected interval")
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("invalid native master selection interval")
    return first, last


def _required_snapshot(saved: InputSnapshot) -> str | None:
    summary = _descriptor(saved)["summary"]
    assert isinstance(summary, dict)
    value = summary.get("snapshot_date")
    if value is not None and not isinstance(value, str):
        raise ValueError("native master selection has an invalid as-of snapshot date")
    return value


def _write_projection(projection: sqlite3.Connection, output: Path) -> None:
    schemas = {
        "jquants_master_snapshots": pa.schema(
            [
                ("snapshot_date", pa.string()),
                ("security_code", pa.string()),
                ("record_json", pa.string()),
            ]
        ),
        "master_receipt_status": pa.schema(
            [
                ("day", pa.string()),
                ("request_id", pa.string()),
                ("status", pa.string()),
                ("row_count", pa.int64()),
                ("failed", pa.int64()),
                ("held", pa.int64()),
                ("confirmed_at", pa.string()),
                ("last_attempt_at", pa.string()),
                ("error", pa.string()),
            ]
        ),
    }
    for table, schema in schemas.items():
        filename = "master_snapshots" if table == "jquants_master_snapshots" else table
        with pq.ParquetWriter(output / (filename + ".parquet"), schema=schema) as writer:
            cursor = projection.execute(f"SELECT * FROM {table} ORDER BY 1,2")  # nosec B608
            while rows := cursor.fetchmany(10000):
                writer.write_table(
                    pa.Table.from_pylist(
                        [dict(zip(schema.names, row, strict=True)) for row in rows], schema=schema
                    )
                )


def rebuild_saved_input(
    store: ObjectStore, saved: InputSnapshot, *, start: date, end: date, output: Path
) -> dict[str, Any]:
    """Cold rebuild uses immutable selection/months/packs only, never latest or old SQLite."""
    first, last = saved_interval(saved)
    if not first <= start <= end <= last:
        raise ValueError("requested interval is outside the saved master selection")
    selected = read_master_input(store, _descriptor(saved), days=_days(start, end))
    scopes = selected["scopes"]
    days = tuple(date.fromisoformat(day) for day in sorted(scopes))
    summary = _descriptor(saved)["summary"]
    assert isinstance(summary, dict)
    clock = summary["evaluated_at"]
    if not isinstance(clock, str):
        raise ValueError("native master selection requires its evaluation clock")
    evaluated_at = datetime.fromisoformat(clock)
    _validate_clock(scopes, evaluated_at)
    output.mkdir(parents=True, exist_ok=False)
    with _projection() as projection:
        result = _project(
            projection,
            read_master_requests(store, scopes=scopes, receipts=selected["receipts"], days=days),
            scopes=scopes,
            evaluated_at=evaluated_at,
            required_snapshot=_required_snapshot(saved),
        )
        _write_projection(projection, output)
    write_json_atomic(
        output / "input.json",
        {
            "saved_input": saved.model_dump(mode="json"),
            "source_head_sha256": _descriptor(saved)["head_sha256"],
            "start": start.isoformat(),
            "end": end.isoformat(),
            **selected,
        },
    )
    return result


def rebuild(
    store: ObjectStore, fixed: FixedMasterL1, *, start: date, end: date, output: Path
) -> dict[str, Any]:
    _days(start, end)
    scopes = {
        day: scope
        for day, scope in fixed.value()["scopes"].items()
        if start <= date.fromisoformat(day) <= end
    }
    days = tuple(date.fromisoformat(day) for day in sorted(scopes))
    output.mkdir(parents=True, exist_ok=False)
    with _projection() as projection:
        result = _project(
            projection,
            fixed.requests(store, days=days),
            scopes=scopes,
            required_snapshot=max(
                (day for day, scope in scopes.items() if scope.get("adopted")), default=None
            ),
        )
        _write_projection(projection, output)
    (output / "head.json").write_bytes(fixed.payload)
    write_json_atomic(
        output / "input.json",
        {
            "head_sha256": fixed.content_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "acquisition": scopes,
        },
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saved-input", type=Path)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store:
            if args.saved_input is not None:
                saved = InputSnapshot.model_validate_json(args.saved_input.read_bytes())
                result = rebuild_saved_input(
                    store, saved, start=args.start, end=args.end, output=args.output
                )
            else:
                fixed = MasterL1(store, args.work_directory).fixed()
                result = rebuild(store, fixed, start=args.start, end=args.end, output=args.output)
            print(json.dumps(result))
    except Exception as error:
        print(f"master native rebuild failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
