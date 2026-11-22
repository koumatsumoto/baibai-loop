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
from types import MappingProxyType
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from pydantic import JsonValue

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    FixedCalendarL1,
    interval,
    verify_calendar_selection,
)
from baibai_engine.machine_analysis.generation.calendar_inputs import (
    CALENDAR_PROJECTION_REVISION,
    INPUT_FORMAT,
    VerifiedCalendarInput,
    calendar_day_usable,
    read_calendar_input,
    retain_calendar_input,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.full_day_inputs import EmptyFullDayInput
from baibai_engine.machine_analysis.observations.prices.storage_contracts import (
    JQUANTS_MARKET_CALENDAR_DDL,
)
from baibai_engine.primitives.jquants_calendar import cash_equity_open
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, ObjectStore
from baibai_engine.storage.sqlite.source_cache.coverage import record_source_coverage
from baibai_engine.storage.sqlite.source_cache.storage_contracts import SOURCE_COVERAGE_DDL

CALENDAR_SOURCE = "jquants_market_calendar"


def _summary(scopes: Mapping[str, Any]) -> dict[str, Any]:
    usable = sum(calendar_day_usable(scope) for scope in scopes.values())
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
            bool(scope.get("valid") and cash_equity_open(scope.get("division")) is None)
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


def _freeze(evidence: dict[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    return MappingProxyType(
        {identity: MappingProxyType(value) for identity, value in evidence.items()}
    )


def _verification_clock(descriptor: dict[str, JsonValue]) -> str:
    summary = descriptor["summary"]
    if not isinstance(summary, dict) or not isinstance(clock := summary.get("evaluated_at"), str):
        raise ValueError("native calendar selection requires its evaluation clock")
    return clock


def prepare_input(
    store: ObjectStore,
    fixed: FixedCalendarL1 | VerifiedCalendarInput,
    *,
    as_of: date,
    evaluated_at: datetime,
    days: Sequence[date] | None = None,
) -> VerifiedCalendarInput:
    """Prove a selection or restrict this operation's already verified full-head evidence."""
    prepared = isinstance(fixed, VerifiedCalendarInput)
    raw_scopes: Mapping[str, Mapping[str, Any]]
    raw_receipts: Mapping[str, Mapping[str, Any]]
    if isinstance(fixed, VerifiedCalendarInput):
        original = _descriptor(fixed.snapshot)
        head_sha256 = original["head_sha256"]
        if not isinstance(head_sha256, str):
            raise ValueError("prepared calendar requires its head digest")
        previous = datetime.fromisoformat(_verification_clock(original))
        if evaluated_at.utcoffset() is None or evaluated_at < previous:
            raise ValueError("prepared calendar is later than the evaluation clock")
        raw_scopes, raw_receipts = fixed.scopes, fixed.receipts
        full_head = fixed.full_head
    else:
        head = fixed.value()
        raw_scopes, raw_receipts = head["days"], head["receipts"]
        if not isinstance(raw_scopes, dict):
            raise ValueError("native calendar L1 days must be an object")
        head_sha256 = fixed.content_id
        full_head = True
    requested = None if days is None else {day.isoformat() for day in days}
    retained = sorted(raw_scopes if requested is None else requested)
    if not retained:
        raise EmptyFullDayInput("native calendar L1 has no retained days")
    selected_days = interval(date.fromisoformat(retained[0]), date.fromisoformat(retained[-1]))
    if not full_head and any(day.isoformat() not in raw_scopes for day in selected_days):
        raise ValueError("requested interval is outside the prepared calendar proof")
    scopes = {
        day: dict(raw_scopes.get(day, {})) if requested is None or day in requested else {}
        for day in (value.isoformat() for value in selected_days)
    }
    identities = {
        identity
        for scope in scopes.values()
        for field in ("adopted", "failure", "held", "confirmation_id")
        if (identity := scope.get(field))
    }
    receipts = {identity: dict(raw_receipts[identity]) for identity in identities}
    if not prepared:
        scopes = verify_calendar_selection(
            store, scopes=scopes, receipts=receipts, evaluated_at=evaluated_at
        )
    descriptor = retain_calendar_input(
        store,
        {
            "head_sha256": head_sha256,
            "as_of": as_of.isoformat(),
            "scopes": scopes,
            "receipts": receipts,
            "summary": {
                "selection_start": retained[0],
                "selection_end": retained[-1],
                "as_of": as_of.isoformat(),
                "evaluated_at": evaluated_at.isoformat(),
                "projection_revision": CALENDAR_PROJECTION_REVISION,
                **_summary(scopes),
            },
        },
        immediate_full_replay=True,
    )
    # Read back every selected immutable month as before, even when price will use
    # only some of them. A matching HEAD declaration does not prove stored bytes.
    if read_calendar_input(store, descriptor, days=selected_days) != {
        "scopes": scopes,
        "receipts": receipts,
    }:
        raise ValueError("retained calendar selection differs from its verified scopes")
    digest = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
    return VerifiedCalendarInput(
        InputSnapshot(
            kind="native_l1",
            reference="jquants-market-calendar-" + digest,
            sha256=digest,
            descriptor=descriptor,
        ),
        _freeze(scopes),
        _freeze(receipts),
        full_head=full_head and days is None,
    )


def fixed_input(
    store: ObjectStore,
    fixed: FixedCalendarL1,
    *,
    as_of: date,
    evaluated_at: datetime,
    days: Sequence[date] | None = None,
) -> InputSnapshot:
    """Save a verified current calendar; the working proof does not escape to disk."""
    return prepare_input(store, fixed, as_of=as_of, evaluated_at=evaluated_at, days=days).snapshot


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
    additional_evidence_days: Sequence[date] = (),
) -> dict[str, Any]:
    """Read only the saved edition and its original packs, never a current head."""
    first, last = saved_interval(saved)
    descriptor = _descriptor(saved)
    summary = descriptor["summary"]
    assert isinstance(summary, dict)
    clock = _verification_clock(descriptor)
    versioned = "projection_revision" in summary
    if versioned and summary["projection_revision"] != CALENDAR_PROJECTION_REVISION:
        raise ValueError("saved calendar projection revision does not match this reader")
    requested = interval(first, last) if days is None else days
    # Some consumers need native evidence after the last projected day (e.g. a
    # weekly balance resolved by Friday). Replaying old editions keeps old bounds.
    verification_days = (
        sorted(set(requested) | set(additional_evidence_days)) if versioned else requested
    )
    selected = read_calendar_input(store, descriptor, days=verification_days)
    scopes = selected["scopes"]
    if (
        verify_calendar_selection(
            store,
            scopes=scopes,
            receipts=selected["receipts"],
            evaluated_at=datetime.fromisoformat(clock),
            verify_current_days=versioned,
        )
        != scopes
    ):
        raise ValueError("saved calendar projection differs from its original day proof")
    return _materialize(
        market,
        {day.isoformat(): scopes[day.isoformat()] for day in requested},
        clock=clock,
        replace_all=replace_all,
    )


def materialize_prepared_input(
    prepared: VerifiedCalendarInput,
    *,
    market: Path,
    days: Sequence[date] | None = None,
    replace_all: bool = True,
) -> dict[str, Any]:
    """Use this operation's checked scopes without repeating pack reads."""
    clock = _verification_clock(_descriptor(prepared.snapshot))
    scopes = (
        prepared.scopes
        if days is None
        else {day.isoformat(): prepared.scopes[day.isoformat()] for day in days}
    )
    return _materialize(market, scopes, clock=clock, replace_all=replace_all)


def _materialize(
    market: Path,
    scopes: Mapping[str, Mapping[str, Any]],
    *,
    clock: str,
    replace_all: bool,
) -> dict[str, Any]:
    with closing(sqlite3.connect(market)) as connection, connection:
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
            usable = calendar_day_usable(scope)
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
    return prepare_model_input(
        store, fixed, as_of=as_of, evaluated_at=evaluated_at, market=market
    ).snapshot


def prepare_model_input(
    store: ObjectStore,
    fixed: FixedCalendarL1,
    *,
    as_of: date,
    evaluated_at: datetime,
    market: Path,
) -> VerifiedCalendarInput:
    prepared = prepare_input(store, fixed, as_of=as_of, evaluated_at=evaluated_at)
    materialize_prepared_input(prepared, market=market)
    return prepared


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
