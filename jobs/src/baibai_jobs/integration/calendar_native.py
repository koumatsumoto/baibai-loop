"""Rebuild the calendar execution view from one retained native day selection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from pydantic import JsonValue

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    FixedCalendarL1,
    interval,
    read_calendar_attempts,
)
from baibai_engine.data_collection.providers.jquants.contracts import complete_request
from baibai_engine.machine_analysis.generation.calendar_inputs import (
    INPUT_FORMAT,
    read_calendar_input,
    retain_calendar_input,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.observations.prices.storage_contracts import (
    JQUANTS_MARKET_CALENDAR_DDL,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, ObjectStore
from baibai_engine.storage.sqlite.source_cache.coverage import record_source_coverage
from baibai_engine.storage.sqlite.source_cache.storage_contracts import SOURCE_COVERAGE_DDL

CALENDAR_SOURCE = "jquants_market_calendar"


def _validate_clock(scopes: Mapping[str, Any], evaluated_at: datetime) -> None:
    if evaluated_at.utcoffset() is None:
        raise ValueError("calendar evaluation clock requires a timezone")
    for scope in scopes.values():
        for field in ("confirmed_at", "last_attempt_at", "changed_at"):
            if value := scope.get(field):
                stamp = datetime.fromisoformat(value)
                if stamp.utcoffset() is None or stamp > evaluated_at:
                    raise ValueError(
                        "calendar input contains receipts later than the evaluation clock"
                    )


def _usable(scope: Mapping[str, Any]) -> bool:
    # Unknown nonblank source categories can satisfy acquisition without proving
    # either an open or a closed cash-equity session. Failed/held newer evidence
    # also prevents the retained old row from being used as holiday proof.
    return bool(
        scope.get("adopted")
        and scope.get("valid")
        and scope.get("division") in {"0", "1", "2", "3"}
        and not scope.get("failure")
        and not scope.get("held")
    )


def _summary(scopes: Mapping[str, Any]) -> dict[str, Any]:
    usable = sum(_usable(scope) for scope in scopes.values())
    raw_complete = sum(
        bool(scope.get("adopted") and scope.get("valid"))
        and not scope.get("failure")
        and not scope.get("held")
        for scope in scopes.values()
    )
    result: dict[str, Any] = {
        "calendar_days": len(scopes),
        "adopted_days": sum(bool(scope.get("adopted")) for scope in scopes.values()),
        "rows": usable,
        "unavailable_days": len(scopes) - usable,
        "failed_days": sum(bool(scope.get("failure")) for scope in scopes.values()),
        "held_days": sum(bool(scope.get("held")) for scope in scopes.values()),
        "invalid_days": sum(
            bool(scope.get("adopted") and not scope.get("valid")) for scope in scopes.values()
        ),
        "unknown_division_days": sum(
            bool(scope.get("valid") and scope.get("division") not in {"0", "1", "2", "3"})
            for scope in scopes.values()
        ),
        "coverage_status": "complete" if usable == len(scopes) else "partial",
        "raw_coverage_status": "complete" if raw_complete == len(scopes) else "partial",
    }
    for name, field, operation in (
        ("oldest_confirmed_at", "confirmed_at", min),
        ("latest_confirmed_at", "confirmed_at", max),
        ("latest_changed_at", "changed_at", max),
    ):
        stamps = [scope[field] for scope in scopes.values() if scope.get(field)]
        result[name] = operation(stamps) if stamps else None
    return result


def fixed_input(
    store: ObjectStore,
    fixed: FixedCalendarL1,
    *,
    as_of: date,
    evaluated_at: datetime,
    days: Sequence[date] | None = None,
) -> InputSnapshot:
    """Retain all selected day evidence; neither as-of nor calendar year trims history."""
    head = fixed.value()
    scopes = head["days"]
    if days is not None:
        scopes = {day.isoformat(): scopes.get(day.isoformat(), {}) for day in days}
    if not scopes:
        raise ValueError("native calendar L1 has no retained days")
    _validate_clock(scopes, evaluated_at)
    retained = sorted(scopes)
    scopes = {
        day.isoformat(): scopes.get(day.isoformat(), {})
        for day in interval(date.fromisoformat(retained[0]), date.fromisoformat(retained[-1]))
    }
    identities = {
        identity
        for scope in scopes.values()
        for field in ("adopted", "failure", "held", "confirmation_id")
        if (identity := scope.get(field))
    }
    descriptor = retain_calendar_input(
        store,
        {
            "head_sha256": fixed.content_id,
            "as_of": as_of.isoformat(),
            "scopes": scopes,
            "receipts": {identity: head["receipts"][identity] for identity in identities},
            "summary": {
                "selection_start": retained[0],
                "selection_end": retained[-1],
                "as_of": as_of.isoformat(),
                "evaluated_at": evaluated_at.isoformat(),
                **_summary(scopes),
            },
        },
    )
    digest = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
    return InputSnapshot(
        kind="native_l1",
        reference="jquants-market-calendar-" + digest,
        sha256=digest,
        descriptor=descriptor,
    )


def _descriptor(saved: InputSnapshot) -> dict[str, JsonValue]:
    saved = InputSnapshot.model_validate_json(saved.model_dump_json())
    if (
        saved.kind != "native_l1"
        or saved.descriptor is None
        or saved.descriptor.get("format") != INPUT_FORMAT
    ):
        raise ValueError("a digest-verified native calendar input is required")
    return saved.descriptor


def saved_interval(saved: InputSnapshot) -> tuple[date, date]:
    summary = _descriptor(saved)["summary"]
    if not isinstance(summary, dict):
        raise ValueError("native calendar input requires its summary")
    start, end = summary.get("selection_start"), summary.get("selection_end")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("native calendar input requires its selection interval")
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("invalid native calendar selection interval")
    return first, last


def materialize_saved_input(
    store: ObjectStore,
    saved: InputSnapshot,
    *,
    market: Path,
    days: Sequence[date] | None = None,
    replace_all: bool = True,
) -> dict[str, Any]:
    """Cold projection reads only fixed monthly metadata and original range packs."""
    first, last = saved_interval(saved)
    descriptor = _descriptor(saved)
    selected = read_calendar_input(
        store, descriptor, days=interval(first, last) if days is None else days
    )
    scopes = selected["scopes"]
    summary = descriptor["summary"]
    assert isinstance(summary, dict)
    clock = summary.get("evaluated_at")
    if not isinstance(clock, str):
        raise ValueError("native calendar selection requires its evaluation clock")
    evaluated_at = datetime.fromisoformat(clock)
    _validate_clock(scopes, evaluated_at)
    adopted = {scope["adopted"] for scope in scopes.values() if scope.get("adopted")}
    identities = {
        identity
        for scope in scopes.values()
        for field in ("adopted", "failure", "held")
        if (identity := scope.get(field))
    }
    # Verify intact source bytes before replacing any execution rows. Day selection
    # is already resolved by L1; replaying whole ranges would reapply a partial
    # request to days that a later, narrower request has successfully recovered.
    for attempt in read_calendar_attempts(
        store, receipts=selected["receipts"], identities=identities
    ):
        clocks = [page.received_at for page in attempt.pages]
        if attempt.attempted_at is not None:
            clocks.append(attempt.attempted_at)
        if any(stamp > evaluated_at for stamp in clocks):
            raise ValueError("calendar input contains receipts later than the evaluation clock")
        if attempt.request_id in adopted:
            complete_request(attempt.pages)
            if any(
                not attempt.start <= date.fromisoformat(day) <= attempt.end
                for day, scope in scopes.items()
                if scope.get("adopted") == attempt.request_id
            ):
                raise ValueError("adopted calendar request differs from its fixed dates")
    with sqlite3.connect(market) as connection:
        # This is a disposable execution copy. The migrated raw authority lives
        # in native packs, not in a second source_pages export on every run.
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jquants_source_pages'"
        ).fetchone():
            connection.execute("DELETE FROM jquants_source_pages WHERE source='market_calendar'")
        if replace_all:
            connection.execute("DELETE FROM jquants_market_calendar")
            connection.execute("DELETE FROM source_coverage WHERE source=?", (CALENDAR_SOURCE,))
        else:
            for day in scopes:
                connection.execute("DELETE FROM jquants_market_calendar WHERE day=?", (day,))
                connection.execute(
                    "DELETE FROM source_coverage WHERE source=? "
                    "AND coverage_start<=? AND coverage_end>=?",
                    (CALENDAR_SOURCE, day, day),
                )
        for day, scope in sorted(scopes.items()):
            usable = _usable(scope)
            if usable:
                connection.execute(
                    "INSERT INTO jquants_market_calendar VALUES (?,?)", (day, scope["division"])
                )
            record_source_coverage(
                connection,
                source=CALENDAR_SOURCE,
                coverage_key="get_mkt_calendar:" + day + ":" + day,
                coverage_start=day,
                coverage_end=day,
                record_count=int(usable),
                status="ok" if usable else "unavailable",
                fetched_at_utc=scope.get("confirmed_at") or scope.get("last_attempt_at") or clock,
                error=None if usable else "native calendar day is not safely usable",
            )
    return _summary(scopes)


def model_input(
    store: ObjectStore,
    fixed: FixedCalendarL1,
    *,
    as_of: date,
    evaluated_at: datetime,
    market: Path,
) -> InputSnapshot:
    saved = fixed_input(store, fixed, as_of=as_of, evaluated_at=evaluated_at)
    materialize_saved_input(store, saved, market=market)
    return saved


def rebuild_saved_input(
    store: ObjectStore, saved: InputSnapshot, *, output: Path
) -> dict[str, Any]:
    """Explicit cold rebuild artifact; its only inputs are immutable selection and packs."""
    first, last = saved_interval(saved)
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix=".calendar-rebuild-") as temporary:
        market = Path(temporary) / "calendar.sqlite"
        with closing(sqlite3.connect(market)) as connection:
            connection.executescript(JQUANTS_MARKET_CALENDAR_DDL + SOURCE_COVERAGE_DDL)
        result = materialize_saved_input(store, saved, market=market)
        schemas = {
            "jquants_market_calendar": pa.schema(
                [("day", pa.string()), ("holiday_division", pa.string())]
            ),
            "source_coverage": pa.schema(
                [
                    ("source", pa.string()),
                    ("coverage_key", pa.string()),
                    ("coverage_start", pa.string()),
                    ("coverage_end", pa.string()),
                    ("fetched_at_utc", pa.string()),
                    ("record_count", pa.int64()),
                    ("status", pa.string()),
                    ("error", pa.string()),
                ]
            ),
        }
        with closing(sqlite3.connect(market)) as connection:
            for table, schema in schemas.items():
                filename = "calendar" if table == "jquants_market_calendar" else "coverage"
                with pq.ParquetWriter(output / (filename + ".parquet"), schema=schema) as writer:
                    cursor = connection.execute(f"SELECT * FROM {table} ORDER BY 1,2")  # nosec B608
                    while rows := cursor.fetchmany(10000):
                        writer.write_table(
                            pa.Table.from_pylist(
                                [dict(zip(schema.names, row, strict=True)) for row in rows],
                                schema=schema,
                            )
                        )
    write_json_atomic(
        output / "input.json",
        {
            "saved_input": saved.model_dump(mode="json"),
            "start": first.isoformat(),
            "end": last.isoformat(),
            "summary": result,
        },
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saved-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        saved = InputSnapshot.model_validate_json(args.saved_input.read_bytes())
        with closing(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        ) as store:
            result = rebuild_saved_input(store, saved, output=args.output)
        print(json.dumps(result))
    except Exception as error:
        print(f"calendar native rebuild failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
