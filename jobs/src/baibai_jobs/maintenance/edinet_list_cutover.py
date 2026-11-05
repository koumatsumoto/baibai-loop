"""固定した旧一覧原本と全handoffを有限移行する。正規化済み応答は原本にしない。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import zlib
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.list_projection import (
    document_list_rechecked_asof,
    project_document_list,
)
from baibai_engine.data_collection.providers.edinet.snapshot_contracts import EDINET_LIST_RESPONSES
from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import (
    _edinet_document_rows,
)
from baibai_engine.machine_analysis.observations.disclosures.snapshot_contracts import (
    EDINET_DOCUMENT_LISTS,
    EDINET_DOCUMENTS,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError
from baibai_engine.storage.snapshots.duck import LakeSession
from baibai_engine.storage.snapshots.objects import LakeObjectCache, open_lake
from baibai_engine.storage.snapshots.reader import (
    FixedRelease,
    accepted_dataset,
    iter_partition_rows,
    materialize_partitions,
    resolve_release,
    selected_partitions,
)
from baibai_jobs.acquisition.receipts import ReceiptStore, available_handoffs, download_fixed

FORMAT = "edinet-list-cutover-v1"
SOURCES = ("edinet-list", "edinet-metrics", "edinet-facts")


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _path(directory: Path, name: str) -> Path:
    path = directory / name
    if path.resolve().is_relative_to(directory.resolve()) and not path.is_symlink():
        return path
    raise ValueError("prepared input escapes migration directory")


def _receipt(item: dict[str, Any], body: bytes) -> NativeListReceipt:
    receipt = NativeListReceipt(
        item["receipt_id"],
        date.fromisoformat(item["doc_date"]),
        datetime.fromisoformat(item["received_at_utc"]),
        body,
    )
    if (
        receipt.doc_date.isoformat() != item["doc_date"]
        or receipt.received_at.isoformat(timespec="microseconds") != item["received_at_utc"]
    ):
        raise ValueError("legacy receipt time is not exact UTC microseconds")
    return receipt


def prepare_migration(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    store: ReceiptStore,
    known_at: datetime,
    directory: Path,
) -> dict[str, Any]:
    """Freeze all historical originals without modifying the source or the target."""
    if known_at.utcoffset() is None:
        raise ValueError("migration boundary requires a timezone")
    directory.mkdir(parents=True, exist_ok=False)
    prepared: dict[str, dict[str, Any]] = {}
    projected_days: set[str] = set()
    blocked: list[dict[str, str]] = []
    inputs: list[dict[str, str]] = []
    legacy_lists: list[tuple[Any, ...]] = []
    legacy_documents: list[tuple[Any, ...]] = []
    entries = [
        entry
        for source in SOURCES
        for entry in available_handoffs(
            store,
            source=source,
            include_integrated=True,
        )
    ]

    def retain(path: Path) -> None:
        inputs.append({"file": str(path.relative_to(directory)), "sha256": _sha(path)})

    def save(rows: Any, origin: str) -> set[str]:
        found: set[str] = set()
        for identity, day, received, body in rows:
            found.add(identity)
            try:
                item = {
                    "receipt_id": identity,
                    "doc_date": day,
                    "received_at_utc": received,
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
                receipt = _receipt(item, body)
                if receipt.received_at > known_at:
                    raise ValueError("receipt is later than the fixed migration boundary")
                if identity in prepared:
                    previous = prepared[identity]
                    if any(previous[key] != value for key, value in item.items()):
                        raise ValueError("same receipt ID has different metadata or bytes")
                    previous["origins"].append(origin)
                    continue
                name = item["sha256"] + ".gz"
                (directory / name).write_bytes(body)
                prepared[identity] = {**item, "file": name, "origins": [origin]}
            except (ValueError, TypeError, OSError, EOFError, zlib.error) as error:
                blocked.append({"origin": origin, "receipt_id": identity, "reason": str(error)})
        return found

    def verify_projection(
        origin: str,
        raw_ids: set[str],
        list_rows: list[tuple[Any, ...]],
        document_rows: list[tuple[Any, ...]],
    ) -> None:
        originals = {
            identity: _receipt(
                prepared[identity], (directory / prepared[identity]["file"]).read_bytes()
            ).decoded()
            for identity in raw_ids
            if identity in prepared
        }
        by_day: dict[date, list[Any]] = defaultdict(list)
        for item in originals.values():
            by_day[item.doc_date].append(item)
        documents: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
        for row in document_rows:
            documents[str(row[0])].append(tuple(row))
        listed = {str(row[0]) for row in list_rows}
        for day in documents.keys() - listed:
            blocked.append(
                {
                    "origin": origin,
                    "day": day,
                    "reason": "legacy documents have no selected receipt provenance",
                }
            )
        for row in list_rows:
            day = str(row[0])
            try:
                selected_id = row[5]
                if selected_id is None:
                    if row[4] is not None or documents[day]:
                        raise ValueError(
                            "legacy normalized list has no selected receipt provenance"
                        )
                    continue
                selected = originals.get(str(selected_id))
                if selected is None or selected.doc_date.isoformat() != day:
                    raise ValueError("legacy selected receipt is missing from retained originals")
                projection = project_document_list(
                    [
                        item
                        for item in by_day[selected.doc_date]
                        if item.received_at <= selected.received_at
                    ]
                )
                if projection.error is not None or projection.selected_receipt is None:
                    raise ValueError(
                        "legacy selected list cannot be reconstructed from retained originals"
                    )
                if selected.received_at.isoformat(timespec="microseconds") != row[3]:
                    raise ValueError("legacy selected receipt clock differs from its original")
                if projection.native_payload != json.loads(row[4]):
                    raise ValueError("legacy list body differs from selected native receipt")
                rebuilt = _edinet_document_rows(day, projection.documents)
                if sorted(rebuilt, key=repr) != sorted(documents[day], key=repr):
                    raise ValueError("legacy filing identity needs unavailable earlier originals")
                if row[7] is not None:
                    proof_day = selected.received_at.astimezone(ZoneInfo("Asia/Tokyo")).date()
                    proof = document_list_rechecked_asof(projection, by_day.get(proof_day, []))
                    if proof is None or proof.isoformat() != row[7]:
                        raise ValueError(
                            "legacy historical recheck lacks its real current-day proof"
                        )
            except (ValueError, TypeError, KeyError) as error:
                blocked.append({"origin": origin, "day": day, "reason": str(error)})

    # No lookback or known-at SQL filter: future receipts block, never disappear.
    for dataset in (EDINET_LIST_RESPONSES, EDINET_DOCUMENT_LISTS, EDINET_DOCUMENTS):
        if dataset.name not in release.dataset_names():
            continue
        dataset = accepted_dataset(release, dataset)
        paths = materialize_partitions(
            cache,
            dataset=dataset,
            partitions=selected_partitions(release, dataset.name),
        )
        frozen = []
        for number, source in enumerate(paths):
            path = directory / f"{dataset.name}-{number:05d}.parquet"
            shutil.copyfile(source, path)
            retain(path)
            frozen.append(path)
        columns = None
        for batch in iter_partition_rows(session, dataset=dataset, paths=frozen, columns=columns):
            if dataset.name == EDINET_LIST_RESPONSES.name:
                save(batch, "release:" + release.release_id)
            else:
                projected_days.update(date.fromisoformat(str(row[0])).isoformat() for row in batch)
                if dataset.name == EDINET_DOCUMENT_LISTS.name:
                    legacy_lists.extend(batch)
                else:
                    legacy_documents.extend(batch)
    verify_projection(
        "release:" + release.release_id, set(prepared), legacy_lists, legacy_documents
    )
    for number, entry in enumerate(entries):
        origin = "stage:" + str(entry["stage_id"])
        if entry.get("invalid"):
            blocked.append({"origin": origin, "reason": "invalid fixed handoff"})
            continue
        try:
            packet = directory / f"stage-{number:05d}"
            download_fixed(store, entry, packet)
            for name in ("receipts.sqlite", "result.json"):
                retain(packet / name)
            report = json.loads((packet / "result.json").read_bytes())
            declared = report.get("list_receipts")
            if (
                not isinstance(declared, list)
                or any(not isinstance(value, str) for value in declared)
                or len(set(declared)) != len(declared)
            ):
                raise ValueError("handoff list receipt identities are unknown")
            with closing(
                sqlite3.connect(
                    (packet / "receipts.sqlite").resolve().as_uri() + "?mode=ro",
                    uri=True,
                )
            ) as conn:
                found = save(
                    conn.execute(
                        "SELECT receipt_id,doc_date,received_at_utc,body_gzip "
                        "FROM edinet_list_responses ORDER BY receipt_id",
                    ),
                    origin,
                )
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'",
                    )
                }
                for table, query in (
                    (
                        "edinet_document_lists",
                        "SELECT DISTINCT doc_date FROM edinet_document_lists",
                    ),
                    ("edinet_documents", "SELECT DISTINCT doc_date FROM edinet_documents"),
                ):
                    if table in tables:
                        projected_days.update(
                            date.fromisoformat(row[0]).isoformat() for row in conn.execute(query)
                        )
                verify_projection(
                    origin,
                    found,
                    list(conn.execute("SELECT * FROM edinet_document_lists"))
                    if "edinet_document_lists" in tables
                    else [],
                    list(conn.execute("SELECT * FROM edinet_documents"))
                    if "edinet_documents" in tables
                    else [],
                )
            if set(declared) != found:
                raise ValueError("handoff declared list receipts differ from actual raw IDs")
        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            sqlite3.DatabaseError,
            LakePublishError,
        ) as error:
            blocked.append({"origin": origin, "reason": str(error)})
    missing = sorted(projected_days - {item["doc_date"] for item in prepared.values()})
    plan = {
        "format": FORMAT,
        "release_id": release.release_id,
        "manifest_sha256": release.manifest_sha256,
        "known_at": known_at.isoformat(),
        "handoffs": entries,
        "inputs": inputs,
        "receipts": sorted(prepared.values(), key=lambda item: item["receipt_id"]),
        "projected_days": sorted(projected_days),
        "missing_raw_days": missing,
        "blocked": blocked,
    }
    write_json_atomic(directory / "plan.json", plan)
    (directory / "plan.sha256").write_text(_sha(directory / "plan.json") + "\n")
    return plan


def _prepared(directory: Path) -> tuple[dict[str, Any], list[NativeListReceipt]]:
    if _sha(directory / "plan.json") != (directory / "plan.sha256").read_text().strip():
        raise ValueError("prepared migration plan changed")
    plan = json.loads((directory / "plan.json").read_bytes())
    if plan["format"] != FORMAT or plan["blocked"] or plan["missing_raw_days"]:
        raise ValueError("migration has unresolved originals or missing_raw_days")
    for item in (*plan["inputs"], *plan["receipts"]):
        if _sha(_path(directory, item["file"])) != item["sha256"]:
            raise ValueError("prepared migration input changed")
    receipts = [
        _receipt(item, _path(directory, item["file"]).read_bytes()) for item in plan["receipts"]
    ]
    if len({item.receipt_id for item in receipts}) != len(receipts):
        raise ValueError("duplicate prepared receipt identity")
    if any(item.received_at > datetime.fromisoformat(plan["known_at"]) for item in receipts):
        raise ValueError("receipt is later than the fixed migration boundary")
    return plan, receipts


def _check_target(fixed: Any, receipts: list[NativeListReceipt], *, complete: bool) -> int:
    expected = {item.receipt_id: item for item in receipts}
    found = set()
    for item in fixed.iter_receipts():
        if item.receipt_id not in expected:
            raise ValueError("target contains acquisitions outside this migration")
        if item != expected[item.receipt_id]:
            raise ValueError("target receipt differs from prepared metadata or bytes")
        found.add(item.receipt_id)
    if complete and found != set(expected):
        raise ValueError("migration readback lacks prepared receipt identities")
    return len(found)


def apply_migration(owner: EdinetListL1, *, directory: Path) -> dict[str, Any]:
    """Validate every prepared input before the first write; allow exact partial retries."""
    _plan, receipts = _prepared(directory)
    fixed = owner.freeze(require_existing=False)
    prior = _check_target(fixed, receipts, complete=False)
    fixed = owner.publish(receipts)
    _check_target(fixed, receipts, complete=True)
    verify_migration(owner, directory=directory)
    result = {
        "receipts": len(receipts),
        "added": len(receipts) - prior,
        "equal": prior,
        "head_sha256": fixed.identity,
    }
    write_json_atomic(directory / "applied.json", result)
    return result


def verify_migration(owner: EdinetListL1, *, directory: Path) -> None:
    """Read every published original; malformed response bodies need no projection."""
    _plan, receipts = _prepared(directory)
    _check_target(owner.freeze(require_existing=True), receipts, complete=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "apply", "verify"))
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
            if args.command == "apply":
                print(
                    json.dumps(
                        apply_migration(
                            EdinetListL1(store, args.work_directory),
                            directory=args.directory,
                        ),
                        sort_keys=True,
                    )
                )
                return 0
            if args.command == "verify":
                verify_migration(EdinetListL1(store, args.work_directory), directory=args.directory)
                return 0
            if not args.release or not args.manifest_sha256 or args.known_at is None:
                raise ValueError("prepare requires exact release, digest and migration boundary")
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
                    {
                        "receipts": len(plan["receipts"]),
                        "blocked": plan["blocked"],
                        "missing_raw_days": plan["missing_raw_days"],
                    },
                    sort_keys=True,
                )
            )
            return int(bool(plan["blocked"] or plan["missing_raw_days"]))
    except Exception as error:
        print(f"EDINET list cutover failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
