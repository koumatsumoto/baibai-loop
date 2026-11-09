"""固定した信用日々公表L1を既存consumer用の有限な実行用copyへ投影する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue

from baibai_engine.data_collection.providers.jquants.margin_alerts_native import (
    FixedMarginAlertsL1,
    MarginAlertsL1,
    read_margin_alert_requests,
    verify_migrated_margin_alert_receipts,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.margin_alerts_inputs import (
    INPUT_FORMAT,
    margin_alerts_input_interval,
    read_margin_alerts_input,
    retain_margin_alerts_input,
)
from baibai_engine.machine_analysis.observations.jquants_decode import (
    JQUANTS_NUMERIC_PROJECTION_REVISION,
)
from baibai_engine.machine_analysis.observations.jquants_writer import store_jquants_margin_alerts
from baibai_engine.machine_analysis.observations.trading_activity.jquants_contract import (
    MARGIN_ALERT_OPERATION,
    MARGIN_ALERT_SOURCE,
)
from baibai_engine.machine_analysis.observations.trading_activity.snapshot_contracts import (
    JQUANTS_MARGIN_ALERTS,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, ObjectStore
from baibai_engine.storage.snapshots.models import DatasetManifest
from baibai_engine.storage.sqlite.source_cache.coverage import (
    range_covered,
    record_range_source_coverage,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.native_projection_cache import verified_cached_scopes


def _days(start: date, end: date) -> tuple[date, ...]:
    if start > end:
        raise ValueError("invalid margin alerts publication interval")
    return tuple(start + timedelta(days=i) for i in range((end - start).days + 1))


def _validate_clock(scopes: dict[str, Any], evaluated_at: datetime) -> None:
    if evaluated_at.utcoffset() is None:
        raise ValueError("evaluation time requires a timezone")
    if any(
        datetime.fromisoformat(stamp) > evaluated_at
        for scope in scopes.values()
        for field in ("confirmed_at", "last_attempt_at", "changed_at")
        if (stamp := scope.get(field))
    ):
        raise ValueError("margin alerts state is later than the evaluation clock")


def fixed_input(
    store: ObjectStore,
    fixed: FixedMarginAlertsL1,
    *,
    start: date,
    end: date,
    evaluated_at: datetime,
) -> InputSnapshot:
    """Fix requested publication days and current native state, not a projected population."""
    head = fixed.value()
    scopes = {day.isoformat(): head["scopes"].get(day.isoformat(), {}) for day in _days(start, end)}
    _validate_clock(scopes, evaluated_at)
    summary: dict[str, Any] = {
        "selection_start": start.isoformat(),
        "selection_end": end.isoformat(),
        "evaluated_at": evaluated_at.isoformat(),
        "publication_days": len(scopes),
        "adopted_requests": sum(bool(scope.get("adopted")) for scope in scopes.values()),
        "unavailable_days": sum(not scope.get("adopted") for scope in scopes.values()),
        "failed_days": sum(bool(scope.get("failure")) for scope in scopes.values()),
        "held_days": sum(bool(scope.get("held")) for scope in scopes.values()),
    }
    summary["raw_coverage_status"] = (
        "partial"
        if summary["unavailable_days"] or summary["failed_days"] or summary["held_days"]
        else "complete"
    )
    descriptor = retain_margin_alerts_input(
        store,
        {
            "head_sha256": fixed.content_id,
            "as_of": end.isoformat(),
            "scopes": scopes,
            "receipts": head["receipts"],
            "summary": summary,
        },
    )
    digest = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
    return InputSnapshot(
        kind="native_l1",
        reference="jquants-margin-alerts-" + digest,
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
        raise ValueError("a digest-verified native margin alerts input is required")
    return saved.descriptor


def saved_interval(saved: InputSnapshot) -> tuple[date, date]:
    summary = _descriptor(saved)["summary"]
    if not isinstance(summary, dict):
        raise ValueError("margin alerts input requires its selection summary")
    first, last = summary.get("selection_start"), summary.get("selection_end")
    if not isinstance(first, str) or not isinstance(last, str):
        raise ValueError("margin alerts input requires its selected publication interval")
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    if start > end:
        raise ValueError("invalid saved margin alerts interval")
    return start, end


def materialize_saved_input(
    store: ObjectStore,
    saved: InputSnapshot,
    *,
    output: Path,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """Create a fresh finite cache for existing readers, never an acquisition input.

    PubDate selects replacement days. AppDate remains a separate identity column,
    including older application dates corrected in a later publication. A valid
    unfiltered response describes the designated subset, not all listed stocks.
    """
    first, last = saved_interval(saved)
    start, end = start or first, end or last
    if not first <= start <= end <= last:
        raise ValueError("requested interval is outside the saved margin alerts selection")
    days = _days(start, end)
    descriptor = _descriptor(saved)
    summary = descriptor["summary"]
    if not isinstance(summary, dict):
        raise ValueError("margin alerts input requires its evaluation clock")
    clock = summary.get("evaluated_at")
    if not isinstance(clock, str):
        raise ValueError("margin alerts input requires its evaluation clock")
    evaluated_at = datetime.fromisoformat(clock)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb"):
        pass  # Never modify an existing cache or a production SQLite path.
    try:
        with closing(open_connection(output)) as connection:
            connection.execute("BEGIN")
            scopes = _project_days(store, saved, connection, days=days, output=output)
            _record_unresolved(connection, scopes, evaluated_at)
            unresolved = [
                day for day in days if not range_covered(connection, MARGIN_ALERT_SOURCE, day, day)
            ]
            result = {
                "rows": connection.execute("SELECT COUNT(*) FROM jquants_margin_alerts").fetchone()[
                    0
                ],
                "publication_days": len(days),
                "unavailable_days": sum(not scope.get("adopted") for scope in scopes.values()),
                "partial_days": sum(
                    bool(scopes[day.isoformat()].get("adopted")) for day in unresolved
                ),
                "coverage_status": "partial" if unresolved else "complete",
                "input_sha256": saved.sha256,
            }
            connection.commit()
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return result


def _project_days(
    store: ObjectStore,
    saved: InputSnapshot,
    connection: sqlite3.Connection,
    *,
    days: tuple[date, ...],
    output: Path,
) -> dict[str, Any]:
    """Replace only requested publication days; the caller owns the transaction."""
    if not days:
        return {}
    descriptor = _descriptor(saved)
    summary = descriptor["summary"]
    assert isinstance(summary, dict)
    evaluated_at = datetime.fromisoformat(str(summary["evaluated_at"]))
    selected = read_margin_alerts_input(store, descriptor, days=days)
    scopes: dict[str, Any] = selected["scopes"]
    _validate_clock(scopes, evaluated_at)
    adopted = {scope["adopted"]: day for day, scope in scopes.items() if scope.get("adopted")}
    found = set()
    # A missing original must remove an untrusted old projection, never preserve it.
    connection.executemany(
        "DELETE FROM jquants_margin_alerts WHERE publication_date=?",
        ((day.isoformat(),) for day in days),
    )
    for item in read_margin_alert_requests(
        store, scopes=scopes, receipts=selected["receipts"], days=days
    ):
        if item.last_received_at is not None and item.last_received_at > evaluated_at:
            raise ValueError("margin alerts original is later than the evaluation clock")
        if (day := adopted.get(item.request_id)) is None:
            continue
        if item.request is None or item.error is not None:
            raise ValueError("adopted margin alerts original is incomplete")
        request = item.request
        if (
            request.source != "margin_alerts"
            or set(request.params) != {"date"}
            or date.fromisoformat(request.params["date"]) != date.fromisoformat(day)
        ):
            raise ValueError("margin alerts original differs from the fixed publication day")
        store_jquants_margin_alerts(
            output,
            request.records(),
            requested_start=date.fromisoformat(day),
            requested_end=date.fromisoformat(day),
            fetched_at_utc=scopes[day].get("confirmed_at") or request.received_at.isoformat(),
            connection=connection,
        )
        found.add(item.request_id)
    if found != set(adopted):
        raise ValueError("margin alerts selection lacks an adopted original")
    return scopes


def _record_unresolved(
    connection: sqlite3.Connection, scopes: dict[str, Any], evaluated_at: datetime
) -> None:
    for day, scope in scopes.items():
        if not scope.get("adopted") or scope.get("failure") or scope.get("held"):
            record_range_source_coverage(
                connection,
                source=MARGIN_ALERT_SOURCE,
                operation=MARGIN_ALERT_OPERATION,
                table="jquants_margin_alerts",
                date_column="publication_date",
                requested_start=date.fromisoformat(day),
                requested_end=date.fromisoformat(day),
                status="partial" if scope.get("adopted") else "unavailable",
                error="native acquisition unresolved",
                fetched_at_utc=scope.get("last_attempt_at") or evaluated_at.isoformat(),
            )


def _verified_cached_scopes(
    store: ObjectStore,
    previous: DatasetManifest | None,
    *,
    market: Path,
    mirror_root: Path,
) -> dict[str, Any]:
    return verified_cached_scopes(
        store,
        previous,
        market=market,
        mirror_root=mirror_root,
        dataset=JQUANTS_MARGIN_ALERTS,
        namespace="jquants-margin-alerts",
        input_format=INPUT_FORMAT,
        read_scopes=lambda descriptor: cast(
            dict[str, Any],
            read_margin_alerts_input(
                store,
                descriptor,
                days=_days(*margin_alerts_input_interval(store, descriptor)),
            )["scopes"],
        ),
    )


def model_input(
    store: ObjectStore,
    fixed: FixedMarginAlertsL1,
    *,
    as_of: date,
    evaluated_at: datetime,
    market: Path,
    mirror_root: Path,
    previous: DatasetManifest | None = None,
) -> InputSnapshot:
    """Use one fixed L1 for consumers; only changed days need raw normalization.

    A prior publication is a verified, disposable optimization. Removing every
    old L2 object or row leaves the same result and input identity from fixed L1.
    """
    head = fixed.value()
    retained = [date.fromisoformat(day) for day in head["scopes"]]
    first = min(retained, default=as_of - timedelta(days=7))
    last = max([as_of, *retained])
    saved = fixed_input(store, fixed, start=first, end=last, evaluated_at=evaluated_at)
    days = _days(first, last)
    scopes = {day.isoformat(): head["scopes"].get(day.isoformat(), {}) for day in days}
    cached = _verified_cached_scopes(store, previous, market=market, mirror_root=mirror_root)
    reusable = {
        day
        for day in days
        if scopes[day.isoformat()].get("adopted")
        and scopes[day.isoformat()].get("adopted") == cached.get(day.isoformat(), {}).get("adopted")
    }
    with closing(open_connection(market)) as connection, connection:
        retained_ids = tuple(
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT request_id FROM jquants_source_pages WHERE source='margin_alerts'"
            )
        )
        verify_migrated_margin_alert_receipts(
            connection,
            retained_ids,
            {identity: receipt["fingerprint"] for identity, receipt in head["receipts"].items()},
        )
        connection.execute("DELETE FROM source_coverage WHERE source=?", (MARGIN_ALERT_SOURCE,))
        connection.execute("DELETE FROM jquants_source_pages WHERE source='margin_alerts'")
        connection.execute(
            "DELETE FROM jquants_margin_alerts WHERE publication_date < ? OR publication_date > ?",
            (first.isoformat(), last.isoformat()),
        )
        for day in sorted(reusable):
            scope = scopes[day.isoformat()]
            record_range_source_coverage(
                connection,
                source=MARGIN_ALERT_SOURCE,
                operation=MARGIN_ALERT_OPERATION,
                table="jquants_margin_alerts",
                date_column="publication_date",
                requested_start=day,
                requested_end=day,
                status="ok",
                error=None,
                fetched_at_utc=scope["confirmed_at"],
            )
        _project_days(
            store,
            saved,
            connection,
            days=tuple(day for day in days if day not in reusable),
            output=market,
        )
        _record_unresolved(connection, scopes, evaluated_at)
        unresolved = [
            day for day in days if not range_covered(connection, MARGIN_ALERT_SOURCE, day, day)
        ]
        descriptor = dict(_descriptor(saved))
        summary = descriptor["summary"]
        assert isinstance(summary, dict)
        descriptor["summary"] = {
            **summary,
            "projection_revision": JQUANTS_NUMERIC_PROJECTION_REVISION,
            "coverage_status": "partial" if unresolved else "complete",
            "partial_days": sum(bool(scopes[day.isoformat()].get("adopted")) for day in unresolved),
        }
    digest = hashlib.sha256(canonical_json(descriptor).encode()).hexdigest()
    return InputSnapshot(
        kind="native_l1",
        reference="jquants-margin-alerts-" + digest,
        sha256=digest,
        descriptor=descriptor,
    )


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
            if args.saved_input:
                saved = InputSnapshot.model_validate_json(args.saved_input.read_bytes())
            else:
                saved = fixed_input(
                    store,
                    MarginAlertsL1(store, args.work_directory).fixed(),
                    start=args.start,
                    end=args.end,
                    evaluated_at=datetime.now(UTC),
                )
            args.output.mkdir(parents=True, exist_ok=False)
            result = materialize_saved_input(
                store,
                saved,
                output=args.output / "margin-alerts.sqlite",
                start=args.start,
                end=args.end,
            )
            write_json_atomic(args.output / "input.json", saved.model_dump(mode="json"))
            write_json_atomic(args.output / "result.json", result)
            print(json.dumps(result, sort_keys=True))
            return int(result["coverage_status"] != "complete")
    except Exception as error:
        print(f"margin alerts native reader failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
