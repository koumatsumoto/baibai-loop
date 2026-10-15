"""Temporary #1520 schema-31 / financial-v1 delivery bridge. No acquisition.

Remove after the reviewed cutover. Runtime readers remain current-only. Machine
uploads stay in r2_transfer.sh; this tool only verifies their inputs and recovery state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from baibai_batch.storage.lake_publish import (
    Boto3R2Store,
    ObjectStore,
    PointerPrecondition,
    publish_l1_release,
    read_pointer_snapshot,
)
from baibai_batch.storage.merge_market_store import (
    _raise_fin_summary_coverage_counts,
    _require_fin_summary_coverage_counts,
)
from baibai_batch.storage.publication_lease import (
    LeaseError,
    PublicationLeaseHandle,
    _load_handle,
    verify_held,
)
from baibai_engine.storage.lake import datasets, models, writer
from baibai_engine.storage.lake.objects import LocalMirrorSource
from baibai_engine.storage.lake.reader import resolve_release
from baibai_engine.storage.lake.release import create_l1_release
from baibai_engine.storage.lake.sources import sha256_file
from baibai_engine.storage.observations.ingest.jquants import store_jquants_fin_summaries
from baibai_engine.storage.observations.lake_origin import (
    LakeStoreOrigin,
    advance_lake_store_origin,
    read_lake_store_origin,
)
from baibai_engine.storage.observations.schema import (
    _SCHEMA_SQL,
    SQLITE_SCHEMA_VERSION,
    validate_current_schema,
)

FIN = "jquants.fin_summaries"
TABLE = "jquants_fin_summaries"
MACHINE_KEYS = ("market.sqlite", "runs.sqlite", "indicators.sqlite")
RAW_SCOPE = {
    "20260810516098": "2026-08-10",
    "20260914535943": "2026-09-14",
    "20260914536003": "2026-09-14",
    "20260914536205": "2026-09-14",
}
LEGACY_COLUMNS = tuple(column.name for column in datasets.JQUANTS_FIN_SUMMARIES.columns[:30])


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def fingerprints(
    path: Path, *, legacy_financial: bool = False, exclude_scope: bool = False
) -> dict[str, dict[str, Any]]:
    result = {}
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        for table in tables:
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({_quote(table)})")]
            if table == TABLE and legacy_financial:
                columns = list(LEGACY_COLUMNS)
            names = ",".join(map(_quote, columns))
            where = (
                " WHERE NOT (ticker='246A' AND disclosed_at IN ('2026-08-10','2026-09-14'))"
                if exclude_scope and table == TABLE
                else ""
            )
            digest, count = hashlib.sha256(), 0
            for row in conn.execute(f"SELECT {names} FROM {_quote(table)}{where} ORDER BY {names}"):  # nosec B608
                digest.update(repr(tuple(row)).encode())
                count += 1
            result[table] = {"columns": columns, "rows": count, "sha256": digest.hexdigest()}
    return result


@contextmanager
def legacy_reader() -> Iterator[None]:
    """Exact v1 registry only within this temporary capture; no runtime fallback."""
    current = datasets.LAKE_DATASETS[FIN]
    policy, version = models.PRODUCTION_RELEASE_POLICY, writer.__dict__["SQLITE_SCHEMA_VERSION"]
    datasets.LAKE_DATASETS[FIN] = replace(current, contract_version=1, columns=current.columns[:30])
    models.PRODUCTION_RELEASE_POLICY = policy.model_copy(
        update={
            "datasets": tuple(
                item.model_copy(update={"accepted_contract_versions": (1,)})
                if item.dataset == FIN
                else item
                for item in policy.datasets
            )
        }
    )
    writer.__dict__["SQLITE_SCHEMA_VERSION"] = 31
    try:
        yield
    finally:
        datasets.LAKE_DATASETS[FIN] = current
        models.PRODUCTION_RELEASE_POLICY = policy
        writer.__dict__["SQLITE_SCHEMA_VERSION"] = version


def convert(source: Path, destination: Path) -> dict[str, Any]:
    if SQLITE_SCHEMA_VERSION != 32 or datasets.LAKE_DATASETS[FIN].contract_version != 2:
        raise ValueError("this temporary tool only targets schema32 / financial-v2")
    if destination.exists():
        raise ValueError("destination already exists")
    before = fingerprints(source, legacy_financial=True)
    with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as original:
        if original.execute("PRAGMA user_version").fetchone()[0] != 31:
            raise ValueError("exact schema 31 required")
        shape = tuple(original.execute(f"PRAGMA table_info({TABLE})"))
        expected = tuple(
            (i, col.name, col.sqlite_type, int(not col.nullable), None, col.primary_key_ordinal)
            for i, col in enumerate(datasets.LAKE_DATASETS[FIN].columns[:30])
        )
        if shape != expected or original.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("source is not the exact schema-31 shape")
        columns = tuple(row[1] for row in original.execute(f"PRAGMA table_info({TABLE})"))
        if (
            columns != LEGACY_COLUMNS
            or original.execute("PRAGMA quick_check").fetchone()[0] != "ok"
        ):
            raise ValueError("invalid schema-31 source")
        with closing(sqlite3.connect(destination)) as copied:
            original.backup(copied)
            start = _SCHEMA_SQL.index(f"CREATE TABLE IF NOT EXISTS {TABLE}(")
            stop = _SCHEMA_SQL.index(";", start) + 1
            copied.execute("BEGIN IMMEDIATE")
            copied.execute(f"ALTER TABLE {TABLE} RENAME TO legacy_fin_summaries")
            copied.execute(_SCHEMA_SQL[start:stop])
            names = ",".join(map(_quote, columns))
            copied.execute(
                f"INSERT INTO {TABLE} ({names},record_key) "  # nosec B608
                f"SELECT {names},'legacy' FROM legacy_fin_summaries"
            )
            copied.execute("DROP TABLE legacy_fin_summaries")
            copied.execute(f"PRAGMA user_version={SQLITE_SCHEMA_VERSION}")
            validate_current_schema(copied)
            copied.commit()
    if fingerprints(destination, legacy_financial=True) != before:
        raise ValueError("conversion changed existing tables or values")
    return before


def repair_four_documents(path: Path, records: list[dict[str, Any]]) -> None:
    if len(records) != 4 or {str(row.get("DiscNo")) for row in records} != set(RAW_SCOPE):
        raise ValueError("only the four reviewed document identities are allowed")
    if any(
        str(row.get("Code")) != "246A0" or row.get("DiscDate") != RAW_SCOPE[str(row["DiscNo"])]
        for row in records
    ):
        raise ValueError("raw document scope disagrees")
    before = fingerprints(path, legacy_financial=True, exclude_scope=True)
    actual_columns = [
        column
        for column in LEGACY_COLUMNS
        if column
        not in {
            "forecast_eps",
            "forecast_profit",
            "forecast_ordinary_profit",
            "dps_forecast_annual",
        }
    ]
    names = ",".join(map(_quote, actual_columns))
    with closing(sqlite3.connect(path)) as conn:
        old = conn.execute(
            f"SELECT {names} FROM {TABLE} WHERE ticker='246A' "  # nosec B608
            "AND disclosed_at IN ('2026-08-10','2026-09-14')"
        ).fetchall()
    for day in sorted(set(RAW_SCOPE.values())):
        store_jquants_fin_summaries(
            path,
            [row for row in records if row["DiscDate"] == day],
            requested_start=date.fromisoformat(day),
            requested_end=date.fromisoformat(day),
            requested_ticker="246A",
        )
    with closing(sqlite3.connect(path)) as conn:
        new = conn.execute(
            f"SELECT {names} FROM {TABLE} WHERE ticker='246A' "  # nosec B608
            "AND disclosed_at IN ('2026-08-10','2026-09-14')"
        ).fetchall()
        if any(row not in new for row in old):
            raise ValueError("repair erased a saved actual observation")
    # Coverage is acquisition authority; preserve every claim, status, timestamp and
    # range. Only the existing count primitive reconciles the added document rows.
    after = fingerprints(path, legacy_financial=True, exclude_scope=True)
    if after != before:
        raise ValueError("repair changed an out-of-scope table or row")
    with closing(sqlite3.connect(path)) as conn:
        _raise_fin_summary_coverage_counts(conn)
        _require_fin_summary_coverage_counts(conn, schema="main")
        conn.commit()


def prepare(
    source: Path,
    destination: Path,
    mirror: Path,
    generations: Mapping[str, str],
    pointer_etag: str,
    records: list[dict[str, Any]],
    *,
    producer: str,
    bundle: Path | None = None,
) -> dict[str, Any]:
    if set(generations) != set(MACHINE_KEYS) or not all(generations.values()) or not pointer_etag:
        raise ValueError("the exact pulled machine generations and pointer ETag are required")
    origin = read_lake_store_origin(source)
    if origin is None:
        raise ValueError("hydrated source must carry its fixed R1 origin")
    with legacy_reader():
        old = resolve_release(
            LocalMirrorSource(mirror),
            origin.release_id,
            manifest_sha256=origin.release_manifest_sha256,
        )
        for manifest in old.dataset_manifests.values():
            writer.validate_snapshot_parity(
                sqlite_path=source, mirror_root=mirror, manifest=manifest
            )
    original_inputs = machine_input_hashes(bundle) if bundle is not None else None
    original_runs = destination.with_name(destination.name + ".original-runs.sqlite")
    if bundle is not None:
        if original_runs.exists():
            raise ValueError("original runs preservation file already exists")
        with (
            closing(
                sqlite3.connect(
                    f"{(bundle / 'screening/runs.sqlite').resolve().as_uri()}?mode=ro", uri=True
                )
            ) as src,
            closing(sqlite3.connect(original_runs)) as dst,
        ):
            src.backup(dst)
    source_tables = convert(source, destination)
    repair_four_documents(destination, records)
    exported = writer.export_market_store(
        sqlite_path=destination,
        mirror_root=mirror,
        producer_git_commit=producer,
        expected_store_origin=origin,
    )
    manifest_path, release = create_l1_release(
        dataset_manifest_paths=[item.manifest_path for item in exported.datasets.values()],
        mirror_root=mirror,
        published_coverage_start={
            name: manifest.coverage_start for name, manifest in old.dataset_manifests.items()
        },
    )
    for item in exported.datasets.values():
        writer.validate_snapshot_parity(
            sqlite_path=destination, mirror_root=mirror, manifest=item.manifest
        )
    return {
        "version": 1,
        "original_machine_inputs": original_inputs,
        "original_runs": str(original_runs) if bundle is not None else None,
        "source_schema": 31,
        "source_tables": source_tables,
        "old_origin": asdict(origin),
        "pointer_etag": pointer_etag,
        "machine_generations": dict(generations),
        "stage_tables": fingerprints(destination),
        "new_origin": {
            "release_id": release.release_id,
            "release_manifest_sha256": sha256_file(manifest_path),
        },
        "release_manifest": manifest_path.relative_to(mirror).as_posix(),
    }


def _check_stage(path: Path, receipt: Mapping[str, Any]) -> None:
    actual = fingerprints(path)
    expected = dict(receipt["stage_tables"])
    actual.pop("lake_store_origin", None)
    expected.pop("lake_store_origin", None)
    if actual != expected:
        raise ValueError("staged data changed after export")


def check_lease(store: ObjectStore, handle: PublicationLeaseHandle, minimum_seconds: int) -> None:
    if minimum_seconds <= 0:
        raise ValueError("a measured publication time budget is required")
    verify_held(store, handle)
    if handle.expires_at_utc - datetime.now(UTC) <= timedelta(seconds=minimum_seconds):
        raise LeaseError("insufficient lease time for the complete publication")


def verify_machine_generation(store: ObjectStore, receipt: Mapping[str, Any]) -> None:
    for key in MACHINE_KEYS:
        remote = store.head(key)
        if remote is None or remote.etag != receipt["machine_generations"][key]:
            raise ValueError(f"machine generation changed: {key}; inspect partial-push state")


def publish(
    path: Path,
    mirror: Path,
    receipt: Mapping[str, Any],
    store: ObjectStore,
    handle: PublicationLeaseHandle,
    minimum_seconds: int,
) -> None:
    _check_stage(path, receipt)
    old, new = LakeStoreOrigin(**receipt["old_origin"]), LakeStoreOrigin(**receipt["new_origin"])
    current = read_pointer_snapshot(store)
    if current.pointer is None or (current.pointer.release_id, current.pointer.manifest_sha256) != (
        old.release_id,
        old.release_manifest_sha256,
    ):
        raise ValueError("current pointer is not captured R1; reconcile instead of republishing")
    if current.precondition.etag != receipt["pointer_etag"]:
        raise ValueError("captured pointer ETag is stale")

    def fence() -> None:
        check_lease(store, handle, minimum_seconds)
        verify_machine_generation(store, receipt)

    fence()
    publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=mirror / receipt["release_manifest"],
        expected_release_sha256=new.release_manifest_sha256,
        store=store,
        pointer_precondition=PointerPrecondition(etag=receipt["pointer_etag"]),
        before_pointer_switch=fence,
    )
    advance_lake_store_origin(path, expected=old, target=new)


def reconcile_origin(
    path: Path,
    mirror: Path,
    receipt: Mapping[str, Any],
    store: ObjectStore,
    handle: PublicationLeaseHandle,
    minimum_seconds: int,
) -> None:
    _check_stage(path, receipt)
    check_lease(store, handle, minimum_seconds)
    current = read_pointer_snapshot(store).pointer
    new = LakeStoreOrigin(**receipt["new_origin"])
    if current is None or (current.release_id, current.manifest_sha256) != (
        new.release_id,
        new.release_manifest_sha256,
    ):
        raise ValueError("remote did not reach the staged R2; do not advance origin")
    resolve_release(
        LocalMirrorSource(mirror), new.release_id, manifest_sha256=new.release_manifest_sha256
    )
    held = read_lake_store_origin(path)
    if held == new:
        return
    advance_lake_store_origin(path, expected=LakeStoreOrigin(**receipt["old_origin"]), target=new)


def machine_input_hashes(bundle: Path) -> dict[str, Any]:
    return {
        key: fingerprints(bundle / relative)
        for key, relative in {
            "market.sqlite": "market/market.sqlite",
            "runs.sqlite": "screening/runs.sqlite",
            "indicators.sqlite": "market_context/indicators.sqlite",
        }.items()
    }


def verify_machine_inputs(bundle: Path, receipt: Mapping[str, Any]) -> None:
    """Check the exact three-key handoff; application is outside this tool."""
    if receipt.get("original_machine_inputs") is None:
        raise ValueError("missing captured original bundle; do not publish machine stores")
    if (
        fingerprints(Path(receipt["original_runs"]))
        != receipt["original_machine_inputs"]["runs.sqlite"]
    ):
        raise ValueError("original runs preservation file changed")
    current = machine_input_hashes(bundle)
    if current["indicators.sqlite"] != receipt["original_machine_inputs"]["indicators.sqlite"]:
        raise ValueError(
            "indicator working copy changed; do not overwrite cloud with an older copy"
        )
    _check_stage(bundle / "market/market.sqlite", receipt)
    if read_lake_store_origin(bundle / "market/market.sqlite") != LakeStoreOrigin(
        **receipt["new_origin"]
    ):
        raise ValueError("machine market origin is not verified R2")
    with closing(
        sqlite3.connect(
            f"{(bundle / 'screening/runs.sqlite').resolve().as_uri()}?mode=ro", uri=True
        )
    ) as conn:
        conn.execute(
            "ATTACH DATABASE ? AS original",
            (f"{Path(receipt['original_runs']).resolve().as_uri()}?mode=ro",),
        )
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM original.sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            columns = [
                row[1] for row in conn.execute(f"PRAGMA original.table_info({_quote(table)})")
            ]
            names = ",".join(map(_quote, columns))
            missing = conn.execute(
                f"SELECT {names} FROM original.{_quote(table)} "  # nosec B608
                f"EXCEPT SELECT {names} FROM main.{_quote(table)} LIMIT 1"
            ).fetchone()
            if missing is not None:
                raise ValueError("runs working copy would discard an original row")


def inspect_machine(
    store: ObjectStore, receipt: Mapping[str, Any], private_stage: Path
) -> dict[str, Any]:
    """Read-only classification, retaining every downloaded generation. No retry PUT."""
    private_stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt_head = store.head("machine-manifest.json")
    receipt_body = store.get_bytes("machine-manifest.json") if receipt_head is not None else None
    result: dict[str, Any] = {}
    for key in MACHINE_KEYS:
        before = store.head(key)
        if before is None:
            raise ValueError(f"machine key missing: {key}")
        path = private_stage / f"{key}.{hashlib.sha256(before.etag.encode()).hexdigest()[:16]}"
        if path.exists():
            raise ValueError("inspection path exists; use a new private stage")
        store.download_file(key, path)
        after = store.head(key)
        if after is None or after.etag != before.etag:
            raise ValueError("machine key changed during recovery inspection")
        result[key] = {
            "etag": before.etag,
            "original_generation": before.etag == receipt["machine_generations"][key],
            "tables": fingerprints(path),
        }
    final = store.head("machine-manifest.json")
    if (final.etag if final else None) != (receipt_head.etag if receipt_head else None):
        raise ValueError("bundle receipt changed during recovery inspection")
    for key in MACHINE_KEYS:
        current = store.head(key)
        if current is None or current.etag != result[key]["etag"]:
            raise ValueError("machine generations changed during recovery inspection")
    keys = json.loads(receipt_body)["keys"] if receipt_body is not None else None
    if keys is not None and set(keys) != set(MACHINE_KEYS):
        raise ValueError("invalid machine receipt key set")
    result["bundle_receipt"] = {
        "etag": receipt_head.etag if receipt_head else None,
        "matches": keys is not None
        and all(keys[key] == result[key]["etag"] for key in MACHINE_KEYS),
        "keys": keys,
    }
    if receipt_body is not None:
        (private_stage / "machine-manifest.json").write_bytes(receipt_body)
    (private_stage / "inspection.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "convert",
            "prepare",
            "publish",
            "reconcile-origin",
            "verify-machine",
            "inspect-machine",
        ),
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--bundle-root", type=Path)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--mirror", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--generations", type=Path)
    parser.add_argument("--pointer-etag")
    parser.add_argument("--raw-page", type=Path, action="append", default=[])
    parser.add_argument("--producer")
    parser.add_argument("--lease-handle", type=Path)
    parser.add_argument("--minimum-remaining-seconds", type=int)
    parser.add_argument("--bucket", default="baibai-stores")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    required = {
        "convert": ("source",),
        "prepare": (
            "source",
            "mirror",
            "receipt",
            "generations",
            "pointer_etag",
            "producer",
            "bundle_root",
        ),
        "publish": ("mirror", "receipt", "lease_handle", "minimum_remaining_seconds"),
        "reconcile-origin": ("mirror", "receipt", "lease_handle", "minimum_remaining_seconds"),
        "verify-machine": ("receipt", "lease_handle", "minimum_remaining_seconds", "bundle_root"),
        "inspect-machine": ("receipt",),
    }
    for name in required[args.action]:
        if getattr(args, name) is None:
            parser.error(f"--{name.replace('_', '-')} is required for {args.action}")
    if args.action == "convert":
        convert(args.source, args.store)
        print("schema 31 to 32 conversion preserved every existing table")
        return
    if args.action == "prepare":
        if args.receipt.exists():
            raise ValueError("receipt exists")
        records = [row for page in args.raw_page for row in json.loads(page.read_bytes())["data"]]
        report = prepare(
            args.source,
            args.store,
            args.mirror,
            json.loads(args.generations.read_bytes()),
            args.pointer_etag,
            records,
            producer=args.producer,
            bundle=args.bundle_root,
        )
        args.receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print("prepared and verified schema32/financial-v2 stage; no remote writes")
        return
    receipt = json.loads(args.receipt.read_bytes())
    store = Boto3R2Store(bucket=args.bucket)
    if args.action == "inspect-machine":
        report = inspect_machine(store, receipt, args.store)
        print(
            json.dumps(
                {
                    key: {"etag": item["etag"], "original_generation": item["original_generation"]}
                    for key, item in report.items()
                    if key in MACHINE_KEYS
                }
            )
        )
        return
    handle = _load_handle(args.lease_handle)
    if args.action == "publish":
        publish(args.store, args.mirror, receipt, store, handle, args.minimum_remaining_seconds)
    elif args.action == "reconcile-origin":
        reconcile_origin(
            args.store, args.mirror, receipt, store, handle, args.minimum_remaining_seconds
        )
    else:
        check_lease(store, handle, args.minimum_remaining_seconds)
        verify_machine_generation(store, receipt)
        verify_machine_inputs(args.bundle_root, receipt)
        _check_stage(args.store, receipt)
        if read_lake_store_origin(args.store) != LakeStoreOrigin(**receipt["new_origin"]):
            raise ValueError("market stage origin is not R2")
        # Full-table verification can be slow: fence again at the handoff to PUT.
        check_lease(store, handle, args.minimum_remaining_seconds)
        verify_machine_generation(store, receipt)
    print("verified exact cutover boundary")


if __name__ == "__main__":
    main()
