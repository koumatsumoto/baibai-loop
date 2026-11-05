"""固定した旧空売り残高報告L1とstageを有限移行する。通常dailyはこの移行を代行しない。

同値確認はnative契約どおりfingerprintだけを保存する。旧release・stageを削除せず、
その確認時のexact bytesを必要とする間は旧原本を保持する。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import zlib
from collections.abc import Iterable, Iterator
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsPage,
    complete_request,
)
from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
    PREFIX,
    ShortSaleReportsAttempt,
    ShortSaleReportsL1,
)
from baibai_engine.data_collection.providers.jquants.snapshot_contracts import JQUANTS_SOURCE_PAGES
from baibai_engine.machine_analysis.observations.trading_activity.snapshot_contracts import (
    JQUANTS_SHORT_SALE_REPORTS,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError
from baibai_engine.storage.snapshots.duck import LakeSession
from baibai_engine.storage.snapshots.objects import LakeObjectCache, open_lake
from baibai_engine.storage.snapshots.reader import (
    FixedRelease,
    accepted_dataset,
    materialize_partitions,
    resolve_release,
    selected_partitions,
)
from baibai_jobs.acquisition.receipts import ReceiptStore, available_handoffs, download_fixed
from baibai_jobs.integration.short_sale_reports_native import fixed_input, materialize_saved_input


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _groups(rows: Iterable[tuple[Any, ...]]) -> Iterator[tuple[str, list[tuple[Any, ...]]]]:
    current: str | None = None
    pages: list[tuple[Any, ...]] = []
    for row in rows:
        identity = str(row[0])
        if current is not None and identity != current:
            yield current, pages
            pages = []
        current = identity
        pages.append(row)
    if current is not None:
        yield current, pages


def _attempt(
    identity: str, rows: list[tuple[Any, ...]], known_at: datetime
) -> ShortSaleReportsAttempt:
    pages = []
    for expected, row in enumerate(rows):
        _, index, source, query, received, body = row
        if index != expected or source != "short_sale_reports":
            raise ValueError("missing page index or mixed source")
        page = JQuantsPage(
            source, json.loads(query), datetime.fromisoformat(received), gzip.decompress(body)
        )
        if page.received_at > known_at:
            raise ValueError("receipt is later than the fixed migration boundary")
        pages.append(page)
    query = dict(pages[0].query)
    if set(query) != {"disc_date"}:
        raise ValueError("legacy request is not an unfiltered full source day")
    # The old contract has real page clocks, not a request start clock.
    return ShortSaleReportsAttempt(
        identity, date.fromisoformat(query["disc_date"]), None, tuple(pages)
    )


def prepare_migration(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    store: ReceiptStore,
    known_at: datetime,
    directory: Path,
) -> dict[str, Any]:
    """Read-only remote preparation. Unknown originals block apply, never become empty success."""
    if known_at.utcoffset() is None:
        raise ValueError("migration boundary requires a timezone")
    directory.mkdir(parents=True, exist_ok=False)
    # 移行は全履歴を検査し、境界後の受領を月partitionごと黙って除外しない。
    paths = materialize_partitions(
        cache,
        dataset=accepted_dataset(release, JQUANTS_SOURCE_PAGES),
        partitions=selected_partitions(release, JQUANTS_SOURCE_PAGES.name),
    )
    entries = available_handoffs(store, source="jquants-market", include_integrated=True)
    prepared: dict[str, dict[str, Any]] = {}
    incomplete: set[str] = set()
    blocked: list[dict[str, str]] = []

    def save(rows: Iterable[tuple[Any, ...]], origin: str) -> set[str]:
        found = set()
        for identity, pages in _groups(rows):
            found.add(identity)
            if not any(row[2] == "short_sale_reports" for row in pages):
                continue
            try:
                attempt = _attempt(identity, pages, known_at)
                try:
                    complete_request(attempt.pages)
                except JQuantsAcquisitionError:
                    incomplete.add(identity)
                payload = _encoded(attempt.value())
                digest = hashlib.sha256(payload).hexdigest()
                if identity in prepared:
                    if prepared[identity]["sha256"] != digest:
                        raise ValueError("legacy request identity has different pages")
                    prepared[identity]["origins"].append(origin)
                    continue
                name = digest + ".json"
                (directory / name).write_bytes(payload)
                prepared[identity] = {
                    "request_id": identity,
                    "file": name,
                    "sha256": digest,
                    "order_time": attempt.order_time.isoformat(),
                    "origins": [origin],
                }
            except (ValueError, TypeError, OSError, EOFError, zlib.error) as error:
                blocked.append({"origin": origin, "request_id": identity, "reason": str(error)})
        return found

    if paths:
        cursor = session.connection.cursor()
        try:
            cursor.execute(
                "SELECT request_id,page_index,source,query_json,received_at_utc,body_gzip "
                "FROM read_parquet($files) WHERE request_id IN (SELECT request_id "
                "FROM read_parquet($files) WHERE source='short_sale_reports') "
                "ORDER BY request_id,page_index",
                {"files": [str(path) for path in paths]},
            )

            def rows() -> Iterator[tuple[Any, ...]]:
                while batch := cursor.fetchmany(256):
                    yield from batch

            save(rows(), "release:" + release.release_id)
        finally:
            cursor.close()
    for number, entry in enumerate(entries):
        origin = "stage:" + str(entry["stage_id"])
        if entry.get("invalid"):
            blocked.append({"origin": origin, "reason": "invalid fixed handoff"})
            continue
        try:
            packet = directory / f"stage-{number:05d}"
            download_fixed(store, entry, packet)
            with closing(
                sqlite3.connect(
                    (packet / "receipts.sqlite").resolve().as_uri() + "?mode=ro", uri=True
                )
            ) as conn:
                found = save(
                    conn.execute(
                        "SELECT request_id,page_index,source,query_json,received_at_utc,body_gzip "
                        "FROM jquants_source_pages ORDER BY request_id,page_index"
                    ),
                    origin,
                )
            declared = entry.get("request_ids")
            if not isinstance(declared, list) or not all(isinstance(x, str) for x in declared):
                raise ValueError("handoff request identities are unknown")
            missing = set(declared) - found
            if missing:
                raise ValueError("handoff declares missing request originals")
            report = json.loads((packet / "result.json").read_bytes())
            acquisition = report.get("acquisition", {})
            if acquisition.get("planning_unknown_count"):
                raise ValueError("handoff reports unresolved acquisition planning scope")
            if acquisition.get("status") == "failed":
                failures = acquisition.get("failed_sources")
                # The market stage also owns master/prices/calendar/margin/short sales.
                # Only explicit source labels establish which acquisition failed;
                # exception names and unavailable originals have unknown scope.
                other_sources = {
                    "master",
                    "market_calendar",
                    "margin_interest",
                    "daily_bars",
                    "margin_alerts",
                    "master_incomplete",
                    "calendar_incomplete",
                    "daily_margin_incomplete",
                }
                if (
                    not isinstance(failures, list)
                    or not failures
                    or any(
                        not isinstance(source, str)
                        or source not in other_sources | {"short_sale_reports"}
                        for source in failures
                    )
                ):
                    raise ValueError("failed market handoff has unknown source scope")
                if "short_sale_reports" in failures and not found.intersection(incomplete):
                    raise ValueError(
                        "failed short-sale reports acquisition has no partial request original"
                    )
        except (ValueError, OSError, sqlite3.DatabaseError, LakePublishError) as error:
            blocked.append({"origin": origin, "reason": str(error)})
    plan = {
        "format": "jquants-short-sale-reports-cutover-v1",
        "release_id": release.release_id,
        "manifest_sha256": release.manifest_sha256,
        "known_at": known_at.isoformat(),
        "handoffs": entries,
        "attempts": sorted(
            prepared.values(), key=lambda item: (item["order_time"], item["request_id"])
        ),
        "blocked": blocked,
    }
    write_json_atomic(directory / "plan.json", plan)
    return plan


def apply_migration(owner: ShortSaleReportsL1, *, directory: Path) -> dict[str, int]:
    """Apply only the inspected finite plan to an initialized, otherwise unused target."""
    plan = json.loads((directory / "plan.json").read_bytes())
    if plan["format"] != "jquants-short-sale-reports-cutover-v1" or plan["blocked"]:
        raise ValueError("migration has unresolved original/scope blockers")
    fixed = owner.resume()
    identities = {item["request_id"] for item in plan["attempts"]}
    if set(fixed.value()["receipts"]) - identities:
        raise ValueError("target already contains acquisitions outside this migration")
    # Verify the whole prepared plan before the first write.
    for item in plan["attempts"]:
        path = directory / item["file"]
        if (
            path.parent != directory
            or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]
        ):
            raise ValueError("prepared short-sale reports original changed")
        attempt = ShortSaleReportsAttempt.from_value(json.loads(path.read_bytes()))
        if (
            attempt.request_id != item["request_id"]
            or attempt.order_time.isoformat() != item["order_time"]
        ):
            raise ValueError("prepared short-sale reports identity/order changed")
    totals = dict.fromkeys(("changed", "equal", "failed", "held", "retried"), 0)
    for offset in range(0, len(plan["attempts"]), 31):
        batch = [
            ShortSaleReportsAttempt.from_value(json.loads((directory / item["file"]).read_bytes()))
            for item in plan["attempts"][offset : offset + 31]
        ]
        for key, values in owner.commit(batch).items():
            totals[key] += len(values)
    verify_migration(owner, directory=directory)
    result = owner.fixed()
    write_json_atomic(directory / "applied.json", {"head_sha256": result.content_id, **totals})
    return totals


def verify_migration(owner: ShortSaleReportsL1, *, directory: Path) -> None:
    """Cold-read every migrated pack, including superseded and partial originals."""
    plan = json.loads((directory / "plan.json").read_bytes())
    head = owner.fixed().value()
    expected = {item["request_id"]: item["sha256"] for item in plan["attempts"]}
    for identity, digest in expected.items():
        if head["receipts"].get(identity, {}).get("fingerprint") != digest:
            raise ValueError("migration readback lacks a prepared request identity")
    packs = {
        head["receipts"][identity]["pack"]
        for identity in expected
        if "pack" in head["receipts"][identity]
    }
    found = set()
    for digest in packs:
        body = owner.store.get_bytes(PREFIX + "packs/" + digest + ".json.gz")
        if hashlib.sha256(body).hexdigest() != digest:
            raise ValueError("migrated original pack failed readback")
        for raw in json.loads(gzip.decompress(body)):
            identity = raw["request_id"]
            if identity in expected:
                if hashlib.sha256(_encoded(raw)).hexdigest() != expected[identity]:
                    raise ValueError("migrated original differs from its prepared bytes")
                found.add(identity)
    if any("pack" in head["receipts"][identity] and identity not in found for identity in expected):
        raise ValueError("migrated pack lacks a prepared original")


def compare_projection(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    owner: ShortSaleReportsL1,
    directory: Path,
    as_of: date,
) -> dict[str, Any]:
    """Compare the fixed legacy projection without using it as an acquisition input."""
    plan = json.loads((directory / "plan.json").read_bytes())
    if (release.release_id, release.manifest_sha256) != (
        plan["release_id"],
        plan["manifest_sha256"],
    ):
        raise ValueError("comparison must use the migration's exact old release")
    dataset = accepted_dataset(release, JQUANTS_SHORT_SALE_REPORTS)
    paths = materialize_partitions(
        cache, dataset=dataset, partitions=selected_partitions(release, dataset.name)
    )
    old = {}
    if paths:
        rows = session.connection.read_parquet([str(path) for path in paths]).project(
            ",".join(column.name for column in JQUANTS_SHORT_SALE_REPORTS.columns)
        )
        for row in rows.fetchall():
            old[tuple(row[:2])] = hashlib.sha256(_encoded(row[2:])).hexdigest()
    fixed = owner.fixed()
    retained_days = {date.fromisoformat(key[0]) for key in old} | {
        date.fromisoformat(day) for day in fixed.value()["scopes"]
    }
    if not retained_days:
        raise ValueError("short-sale reports comparison requires retained disclosure days")
    first, last = min(retained_days), max(as_of, max(retained_days))
    # The finite cutover boundary fixes what was actually received. It is not
    # derived from DiscDate or CalcDate, and later queries never advance it silently.
    saved = fixed_input(
        owner.store,
        fixed,
        start=first,
        end=last,
        evaluated_at=datetime.fromisoformat(plan["known_at"]),
    )
    with tempfile.TemporaryDirectory(prefix=".short-sale-reports-compare-") as temporary:
        database = Path(temporary) / "comparison.sqlite"
        result = materialize_saved_input(owner.store, saved, output=database)
        with closing(sqlite3.connect(database)) as connection:
            new = {
                tuple(row[:2]): hashlib.sha256(_encoded(row[2:])).hexdigest()
                for row in connection.execute(
                    "SELECT * FROM jquants_short_sale_reports ORDER BY disclosed_at,source_ordinal"
                )
            }
    report = {
        "release_id": release.release_id,
        "manifest_sha256": release.manifest_sha256,
        "head_sha256": fixed.content_id,
        "as_of": as_of.isoformat(),
        **result,
        "legacy_rows": len(old),
        "native_rows": len(new),
        "matched_rows": sum(old[key] == new[key] for key in old.keys() & new.keys()),
        "differences": [
            {"key": list(key), "legacy_sha256": old.get(key), "native_sha256": new.get(key)}
            for key in sorted(old.keys() | new.keys())
            if old.get(key) != new.get(key)
        ],
    }
    write_json_atomic(directory / "comparison.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "apply", "verify", "compare"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--release")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--known-at", type=datetime.fromisoformat)
    parser.add_argument(
        "--asof", type=date.fromisoformat, help="first normal disclosure date for compare"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bucket = os.environ.get("R2_STORES_BUCKET") or "baibai-stores"
        with closing(Boto3R2Store(bucket=bucket)) as store:
            if args.command == "verify":
                verify_migration(
                    ShortSaleReportsL1(store, args.work_directory), directory=args.directory
                )
                return 0
            if args.command == "apply":
                result = apply_migration(
                    ShortSaleReportsL1(store, args.work_directory), directory=args.directory
                )
                print(json.dumps(result, sort_keys=True))
                return int(bool(result["held"]))
            if args.command == "compare":
                if args.asof is None:
                    raise ValueError("compare requires --asof for the intended daily disclosure")
                plan = json.loads((args.directory / "plan.json").read_bytes())
                with open_lake(mirror=args.work_directory, bucket=bucket) as (session, cache):
                    release = resolve_release(
                        cache.source, plan["release_id"], manifest_sha256=plan["manifest_sha256"]
                    )
                    report = compare_projection(
                        session,
                        release=release,
                        cache=cache,
                        owner=ShortSaleReportsL1(store, args.work_directory),
                        directory=args.directory,
                        as_of=args.asof,
                    )
                print(
                    json.dumps(
                        {
                            "legacy_rows": report["legacy_rows"],
                            "native_rows": report["native_rows"],
                            "differences": len(report["differences"]),
                            "coverage_status": report["coverage_status"],
                        },
                        sort_keys=True,
                    )
                )
                return int(bool(report["differences"]) or report["coverage_status"] != "complete")
            if not args.release or not args.manifest_sha256 or args.known_at is None:
                raise ValueError(
                    "prepare requires the exact release, digest and migration boundary"
                )
            with open_lake(mirror=args.work_directory, bucket=bucket) as (session, cache):
                release = resolve_release(
                    cache.source, args.release, manifest_sha256=args.manifest_sha256
                )
                plan = prepare_migration(
                    session,
                    release=release,
                    cache=cache,
                    store=store,
                    known_at=args.known_at,
                    directory=args.directory,
                )
            print(
                json.dumps(
                    {"requests": len(plan["attempts"]), "blocked": plan["blocked"]}, sort_keys=True
                )
            )
            return int(bool(plan["blocked"]))
    except Exception as error:
        print(f"short-sale reports cutover failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
