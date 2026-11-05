"""固定した旧財務L1とstageを有限移行する。通常dailyはこの移行を代行しない。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import sys
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
from baibai_engine.data_collection.providers.jquants.financial_native import (
    PREFIX,
    FinancialAttempt,
    FinancialL1,
)
from baibai_engine.data_collection.providers.jquants.lake_receipts import fixed_receipt_paths
from baibai_engine.machine_analysis.observations.financials.from_l1 import rebuild_financial_days
from baibai_engine.machine_analysis.observations.financials.snapshot_contracts import (
    JQUANTS_FIN_SUMMARIES,
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


def _attempt(identity: str, rows: list[tuple[Any, ...]], known_at: datetime) -> FinancialAttempt:
    pages = []
    for expected, row in enumerate(rows):
        _, index, source, query, received, body = row
        if index != expected or source != "financial_summaries":
            raise ValueError("missing page index or mixed source")
        page = JQuantsPage(
            source, json.loads(query), datetime.fromisoformat(received), gzip.decompress(body)
        )
        if page.received_at > known_at:
            raise ValueError("receipt is later than the fixed migration boundary")
        pages.append(page)
    query = dict(pages[0].query)
    if set(query) != {"date"}:
        raise ValueError("legacy request is not an unfiltered full source day")
    # The old contract has real page clocks, not a request start clock.
    return FinancialAttempt(identity, date.fromisoformat(query["date"]), None, tuple(pages))


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
    paths = fixed_receipt_paths(release=release, cache=cache, known_at=known_at)
    entries = available_handoffs(store, source="jquants-financial", include_integrated=True)
    prepared: dict[str, dict[str, Any]] = {}
    incomplete: set[str] = set()
    blocked: list[dict[str, str]] = []

    def save(rows: Iterable[tuple[Any, ...]], origin: str) -> set[str]:
        found = set()
        for identity, pages in _groups(rows):
            found.add(identity)
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
                "FROM read_parquet($files) WHERE source='financial_summaries') "
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
            declared = entry.get("request_ids", [])
            if not isinstance(declared, list):
                raise ValueError("handoff request identities are unknown")
            missing = set(declared) - found
            if missing:
                raise ValueError("handoff declares missing request originals")
            report = json.loads((packet / "result.json").read_bytes())
            if report.get("acquisition", {}).get("planning_unknown_count"):
                raise ValueError("handoff reports unresolved acquisition planning scope")
            if report.get("acquisition", {}).get("status") == "failed" and not found.intersection(
                incomplete
            ):
                raise ValueError("failed handoff has no request original or known request scope")
        except (ValueError, OSError, sqlite3.DatabaseError, LakePublishError) as error:
            blocked.append({"origin": origin, "reason": str(error)})
    plan = {
        "format": "jquants-financial-cutover-v1",
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


def apply_migration(owner: FinancialL1, *, directory: Path) -> dict[str, int]:
    """Apply only the inspected finite plan to an initialized, otherwise unused target."""
    plan = json.loads((directory / "plan.json").read_bytes())
    if plan["format"] != "jquants-financial-cutover-v1" or plan["blocked"]:
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
            raise ValueError("prepared financial original changed")
        attempt = FinancialAttempt.from_value(json.loads(path.read_bytes()))
        if (
            attempt.request_id != item["request_id"]
            or attempt.order_time.isoformat() != item["order_time"]
        ):
            raise ValueError("prepared financial identity/order changed")
    totals = dict.fromkeys(("changed", "equal", "failed", "held", "retried"), 0)
    for offset in range(0, len(plan["attempts"]), 31):
        batch = [
            FinancialAttempt.from_value(json.loads((directory / item["file"]).read_bytes()))
            for item in plan["attempts"][offset : offset + 31]
        ]
        for key, values in owner.commit(batch).items():
            totals[key] += len(values)
    verify_migration(owner, directory=directory)
    result = owner.fixed()
    write_json_atomic(directory / "applied.json", {"head_sha256": result.content_id, **totals})
    return totals


def verify_migration(owner: FinancialL1, *, directory: Path) -> None:
    """Cold-read every migrated pack, including superseded and partial originals."""
    plan = json.loads((directory / "plan.json").read_bytes())
    head = owner.fixed().value()
    if head["pending"] is not None:
        raise ValueError("migration has an unconfirmed commit")
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
    owner: FinancialL1,
    directory: Path,
) -> dict[str, Any]:
    """Compare finite old projection with native rebuild, without treating either as an original."""
    plan = json.loads((directory / "plan.json").read_bytes())
    if (release.release_id, release.manifest_sha256) != (
        plan["release_id"],
        plan["manifest_sha256"],
    ):
        raise ValueError("comparison must use the migration's exact old release")
    dataset = accepted_dataset(release, JQUANTS_FIN_SUMMARIES)
    paths = materialize_partitions(
        cache, dataset=dataset, partitions=selected_partitions(release, dataset.name)
    )
    old = {}
    if paths:
        for row in session.connection.execute(
            "SELECT ticker, disclosed_at, disclosure_number, document_payload FROM read_parquet(?)",
            [[str(path) for path in paths]],
        ).fetchall():
            old[tuple(row[:3])] = hashlib.sha256(_encoded(json.loads(row[3]))).hexdigest()
    fixed = owner.fixed()
    days = {date.fromisoformat(key[1]) for key in old} | {
        date.fromisoformat(day) for day in fixed.value()["scopes"]
    }
    rebuilt = rebuild_financial_days(fixed.requests(owner.store, days=sorted(days)), days=days)
    new = {
        tuple(row[:3]): hashlib.sha256(_encoded(json.loads(row[3]))).hexdigest()
        for item in rebuilt.values()
        for row in item.rows
    }
    report = {
        "release_id": release.release_id,
        "manifest_sha256": release.manifest_sha256,
        "head_sha256": fixed.content_id,
        "legacy_rows": len(old),
        "native_rows": len(new),
        "matched_rows": sum(old[key] == new[key] for key in old.keys() & new.keys()),
        "differences": [
            {"key": list(key), "legacy_sha256": old.get(key), "native_sha256": new.get(key)}
            for key in sorted(old.keys() | new.keys())
            if old.get(key) != new.get(key)
        ],
        "days": {
            day.isoformat(): {"status": item.status, "incomplete": item.incomplete_requests}
            for day, item in rebuilt.items()
        },
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bucket = os.environ.get("R2_STORES_BUCKET") or "baibai-stores"
        with closing(Boto3R2Store(bucket=bucket)) as store:
            if args.command == "verify":
                verify_migration(FinancialL1(store, args.work_directory), directory=args.directory)
                return 0
            if args.command == "apply":
                result = apply_migration(
                    FinancialL1(store, args.work_directory), directory=args.directory
                )
                print(json.dumps(result, sort_keys=True))
                return int(bool(result["held"]))
            if args.command == "compare":
                plan = json.loads((args.directory / "plan.json").read_bytes())
                with open_lake(mirror=args.work_directory, bucket=bucket) as (session, cache):
                    release = resolve_release(
                        cache.source, plan["release_id"], manifest_sha256=plan["manifest_sha256"]
                    )
                    report = compare_projection(
                        session,
                        release=release,
                        cache=cache,
                        owner=FinancialL1(store, args.work_directory),
                        directory=args.directory,
                    )
                print(
                    json.dumps(
                        {
                            "legacy_rows": report["legacy_rows"],
                            "native_rows": report["native_rows"],
                            "differences": len(report["differences"]),
                        },
                        sort_keys=True,
                    )
                )
                return 0
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
        print(f"financial cutover failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
