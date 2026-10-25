"""Provider-scoped acquisition and append-only handoff; publication reads fresh stores."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.job_api import (
    DEFAULT_ECONOMIC_DB_PATH,
    EconomicSchemaError,
    EconomicSourceRefreshFailure,
    economic_acquisition_groups,
    economic_explicit_recovery_ranges,
    economic_source_policy,
    export_economic_receipts,
    load_economic_sources,
    refresh_economic_sources,
    retain_economic_rechecks,
    validate_economic_schema,
)
from baibai_jobs.observability.discord import write_json_atomic
from baibai_jobs.storage.economic_staging import load_handoff, save_handoff
from baibai_jobs.storage.lake_publish import Boto3R2Store, LakePublishError
from baibai_jobs.storage.merge_economic_store import FACT_KEYS, merge_stores, verify_stores
from baibai_jobs.storage.store_merge import MergeError


def selected_sources(group: str, source_id: str = "") -> list[str]:
    members = sorted(
        source.source_id
        for source in load_economic_sources().values()
        if economic_source_policy(source).acquisition_group == group
    )
    if not members or (source_id and source_id not in members):
        raise ValueError("source must belong to the selected acquisition group")
    return [source_id] if source_id else members


def acquire(group: str, source_id: str, asof: date, directory: Path, database: Path) -> int:
    sources = selected_sources(group, source_id)
    # Missing canonical input is a provisioning failure, never an implicit empty seed.
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        validate_economic_schema(conn)
    since = datetime.now(UTC)
    with retain_economic_rechecks():
        outcomes = refresh_economic_sources(
            sources, db_path=database, start=None, end=asof, recent=True
        )
    directory.mkdir(parents=True, exist_ok=True)
    export_economic_receipts(
        database, directory / "receipts.sqlite", source_ids=sources, since=since
    )
    rows: list[dict[str, object]] = []
    catalog = load_economic_sources()
    for outcome in outcomes:
        row: dict[str, object] = {"source_id": outcome.source_id}
        if isinstance(outcome, EconomicSourceRefreshFailure):
            row["status"] = "failed"
        else:
            row.update(
                status="ok",
                start=outcome.start.isoformat(),
                end=outcome.end.isoformat(),
                record_count=outcome.record_count,
            )
        rows.append(row)
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        for row in rows:
            row["history_recovery_required"] = bool(
                economic_explicit_recovery_ranges(conn, catalog[str(row["source_id"])], asof)
            )
    report = {
        "group": group,
        "started_at": since.isoformat(),
        "asof": asof.isoformat(),
        "sources": rows,
    }
    # Only public source identity/ranges/counts cross jobs; provider error text stays local.
    write_json_atomic(directory / "result.json", report)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as stream:
            print(f"report={json.dumps(report, ensure_ascii=False)}", file=stream)
            print("stage_ready=true", file=stream)
    print(json.dumps(report, ensure_ascii=False))
    return int(any(isinstance(item, EconomicSourceRefreshFailure) for item in outcomes))


def receive(directory: Path, database: Path, group: str, source_id: str, *, verify: bool) -> None:
    expected = selected_sources(group, source_id)
    report = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if report["group"] != group or [row["source_id"] for row in report["sources"]] != expected:
        raise ValueError("economic handoff selection differs from the requested source plan")
    source = directory / "receipts.sqlite"
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        validate_economic_schema(conn)
        for table in FACT_KEYS:
            actual = {row[0] for row in conn.execute(f"SELECT DISTINCT source_id FROM {table}")}  # nosec B608
            if not actual <= set(expected):
                raise ValueError("economic handoff contains another acquisition group's receipts")
    if verify:
        verify_stores(source, database)
        print("economic publication readback verified")
    else:
        print(merge_stores(source, database).render())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("plan", "input", "acquire", "stage", "download", "merge", "verify")
    )
    parser.add_argument("--group", default="all")
    parser.add_argument("--source-id", default="")
    parser.add_argument("--asof", type=date.fromisoformat)
    parser.add_argument("--run-created-at", type=datetime.fromisoformat)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--stage-id")
    parser.add_argument("--db", type=Path, default=DEFAULT_ECONOMIC_DB_PATH)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            groups = economic_acquisition_groups() if args.group == "all" else (args.group,)
            for group in groups:
                selected_sources(group, args.source_id)
            matrix = json.dumps([{"group": group} for group in groups])
            created = args.run_created_at or datetime.now(UTC)
            if created.tzinfo is None:
                raise ValueError("original run clock requires a timezone")
            asof = args.asof or created.astimezone(ZoneInfo("Asia/Tokyo")).date()
            if output := os.environ.get("GITHUB_OUTPUT"):
                with Path(output).open("a", encoding="utf-8") as stream:
                    print(f"groups={matrix}\nasof={asof.isoformat()}", file=stream)
            print(matrix)
            return 0
        if args.directory is None:
            raise ValueError("--directory is required")
        if args.command == "input":
            from baibai_jobs.storage.source_inputs import load_input

            selected_sources(args.group, args.source_id)
            load_input(
                Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores"),
                "economic-" + args.group,
                args.db,
                args.directory,
            )
            return 0
        if args.command in {"stage", "download"}:
            if args.stage_id is None:
                raise ValueError("--stage-id is required")
            store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
            if args.command == "stage":
                identity = save_handoff(store, args.stage_id, args.directory)
                if output := os.environ.get("GITHUB_OUTPUT"):
                    with Path(output).open("a", encoding="utf-8") as stream:
                        print(f"handoff={identity}", file=stream)
            else:
                load_handoff(store, args.stage_id, args.directory)
            return 0
        if args.command == "acquire":
            if args.asof is None:
                raise ValueError("--asof is required")
            return acquire(args.group, args.source_id, args.asof, args.directory, args.db)
        receive(
            args.directory, args.db, args.group, args.source_id, verify=args.command == "verify"
        )
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        KeyError,
        EconomicSchemaError,
        MergeError,
        LakePublishError,
    ) as error:
        print(f"economic handoff failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
