"""固定した旧archive/scopeを既存L1へ有限移行する。取得・通常切替・削除はしない。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.edinet.archive_native import (
    ArchiveMetadata,
    EdinetArchiveL1,
    FixedEdinetArchives,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.facts_scope import FactsScope, read_scope
from baibai_engine.data_collection.providers.edinet.snapshot_contracts import EDINET_SOURCE_ARCHIVES
from baibai_engine.storage.json import canonical_json, write_json_atomic
from baibai_engine.storage.objects.r2 import ObjectStore
from baibai_engine.storage.snapshots.hydrate import expected_row_totals, plan_release_load
from baibai_engine.storage.snapshots.reader import FixedRelease
from baibai_engine.storage.sqlite.source_cache.article_upgrade import upgrade_article_working_copy
from baibai_engine.storage.sqlite.source_cache.facts_scope_upgrade import (
    upgrade_facts_scope_working_copy,
)
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    read_lake_store_origin_from_connection,
)
from baibai_engine.storage.sqlite.source_cache.schema import validate_current_schema
from baibai_jobs.acquisition.receipts import ReceiptStore, available_handoffs, download_fixed

_FORMAT = "edinet-archive-import-v1"
_SOURCES = ("edinet-list", "edinet-metrics", "edinet-facts")


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _scope_value(scope: FactsScope) -> dict[str, Any]:
    return {
        "since": None if scope.since is None else scope.since.isoformat(),
        "doc_ids": sorted(scope.doc_ids),
        "legacy_origins": sorted(scope.legacy_origins),
    }


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        validate_current_schema(connection)
        return connection
    except Exception:
        connection.close()
        raise


def _catalog(connection: sqlite3.Connection) -> dict[str, Any]:
    archives = []
    for doc_id, source_type, stored_on, body in connection.execute(
        "SELECT doc_id,source_type,stored_on,zip_content FROM edinet_source_archives "
        "ORDER BY doc_id,source_type"
    ):
        day = date.fromisoformat(stored_on)
        if day.isoformat() != stored_on or not isinstance(body, bytes):
            raise ValueError("invalid legacy EDINET archive date or bytes")
        metadata = ArchiveMetadata(
            doc_id=doc_id,
            source_type=source_type,
            stored_on=day,
            received_at_utc=None,  # A legacy day never proves an exact receipt clock.
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
        )
        archives.append(metadata.model_dump(mode="json"))
    return {"archives": archives, "acquisition_scope": _scope_value(read_scope(connection))}


def _union(catalogs: list[dict[str, Any]]) -> dict[str, Any]:
    selected: dict[tuple[str, int], dict[str, Any]] = {}
    scope = FactsScope()
    for database, catalog in enumerate(catalogs):
        value = catalog["acquisition_scope"]
        scope = scope.join(
            FactsScope(
                None if value["since"] is None else date.fromisoformat(value["since"]),
                frozenset(value["doc_ids"]),
                frozenset(value["legacy_origins"]),
            )
        )
        for metadata in catalog["archives"]:
            key = metadata["doc_id"], metadata["source_type"]
            previous = selected.get(key)
            if previous is not None and previous["metadata"]["sha256"] != metadata["sha256"]:
                raise ValueError(f"legacy EDINET archive has conflicting bytes: {key}")
            # Canonical and unintegrated stages can reach us out of order. Keep
            # the earliest actual storage day, never the import day's timestamp.
            if previous is None or metadata["stored_on"] < previous["metadata"]["stored_on"]:
                selected[key] = {"database": database, "metadata": metadata}
    return {
        "archives": [selected[key] for key in sorted(selected)],
        "acquisition_scope": _scope_value(scope),
    }


def prepare_import(
    store: ReceiptStore, *, source: Path, release: FixedRelease, directory: Path
) -> dict[str, Any]:
    """Freeze all archives and scope, including sealed/late stages; do not write L1."""
    directory.mkdir(parents=True, exist_ok=False)
    entries = [
        entry
        for name in _SOURCES
        for entry in available_handoffs(store, source=name, market=True, include_integrated=True)
    ]
    databases: list[dict[str, Any]] = []
    catalogs: list[dict[str, Any]] = []
    with closing(_connect(source)) as connection:
        if read_lake_store_origin_from_connection(connection) != LakeStoreOrigin(
            release.release_id, release.manifest_sha256
        ):
            raise ValueError("legacy canonical differs from the fixed release")
        expected = 0
        if EDINET_SOURCE_ARCHIVES.name in release.dataset_names():
            load, _ = plan_release_load(
                release,
                catalog={EDINET_SOURCE_ARCHIVES.name: EDINET_SOURCE_ARCHIVES},
                dataset_names=(EDINET_SOURCE_ARCHIVES.name,),
            )
            expected = expected_row_totals(load).get(EDINET_SOURCE_ARCHIVES.name, 0)
        catalog = _catalog(connection)
        if len(catalog["archives"]) != expected:
            raise ValueError("hydrate all fixed EDINET archives before preparing the import")
    catalogs.append(catalog)
    databases.append({"path": str(source.resolve()), "catalog_sha256": _digest(catalog)})
    for number, entry in enumerate(entries):
        if entry.get("invalid"):
            raise ValueError(f"invalid fixed EDINET stage: {entry['stage_id']}")
        packet = directory / "inputs" / f"stage-{number:05d}"
        download_fixed(store, entry, packet)
        report_path, database_path = packet / "result.json", packet / "receipts.sqlite"
        files = [database_path, report_path]
        # Old immutable integrated stages remain old. Convert only local copies,
        # using the existing finite converters and their stage scope semantics.
        with closing(
            sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as old:
            version = old.execute("PRAGMA user_version").fetchone()[0]
        if version == 48:
            converted = packet / "schema49.sqlite"
            upgrade_article_working_copy(database_path, converted)
            database_path, version = converted, 49
            files.append(converted)
        if version == 49:
            converted = packet / "schema50.sqlite"
            upgrade_facts_scope_working_copy(database_path, converted, stage=True)
            database_path = converted
            files.append(converted)
        report = json.loads(report_path.read_bytes())
        with closing(_connect(database_path)) as connection:
            catalog = _catalog(connection)
        declared = report.get("archives")
        if (
            report.get("source") != entry["source"]
            or not isinstance(declared, list)
            or any(
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or type(item[1]) is not int
                for item in declared
            )
            or sorted(declared)
            != [[item["doc_id"], item["source_type"]] for item in catalog["archives"]]
        ):
            raise ValueError(f"EDINET stage archive declaration differs: {entry['stage_id']}")
        catalogs.append(catalog)
        databases.append(
            {
                "path": str(database_path.resolve()),
                "catalog_sha256": _digest(catalog),
                "files": [{"path": str(path.resolve()), "sha256": _sha(path)} for path in files],
                "handoff": entry,
            }
        )
    plan = {
        "format": _FORMAT,
        "release_id": release.release_id,
        "manifest_sha256": release.manifest_sha256,
        "databases": databases,
        **_union(catalogs),
    }
    write_json_atomic(directory / "import-plan.json", plan)
    (directory / "import-plan.sha256").write_text(_sha(directory / "import-plan.json") + "\n")
    return plan


def _plan(directory: Path) -> dict[str, Any]:
    if (
        _sha(directory / "import-plan.json")
        != (directory / "import-plan.sha256").read_text().strip()
    ):
        raise ValueError("prepared EDINET archive import changed")
    plan: dict[str, Any] = json.loads((directory / "import-plan.json").read_bytes())
    if plan["format"] != _FORMAT or not plan["databases"]:
        raise ValueError("invalid EDINET archive import plan")
    return plan


@contextmanager
def _inputs(plan: dict[str, Any]) -> Iterator[list[sqlite3.Connection]]:
    """Check every source before writing; hold each read snapshot through publish."""
    with ExitStack() as stack:
        connections, catalogs = [], []
        for index, item in enumerate(plan["databases"]):
            path = Path(item["path"])
            for original in item.get("files", []):
                if _sha(Path(original["path"])) != original["sha256"]:
                    raise ValueError("prepared EDINET stage changed")
            connection = stack.enter_context(closing(_connect(path)))
            if index == 0 and read_lake_store_origin_from_connection(connection) != LakeStoreOrigin(
                plan["release_id"], plan["manifest_sha256"]
            ):
                raise ValueError("prepared legacy canonical binding changed")
            catalog = _catalog(connection)
            if _digest(catalog) != item["catalog_sha256"]:
                raise ValueError("prepared EDINET archives or scope changed")
            connections.append(connection)
            catalogs.append(catalog)
        if _union(catalogs) != {
            "archives": plan["archives"],
            "acquisition_scope": plan["acquisition_scope"],
        }:
            raise ValueError("prepared EDINET union differs from its originals")
        yield connections


def _expected(store: ObjectStore, plan: dict[str, Any]) -> FixedEdinetArchives:
    return FixedEdinetArchives(
        store,
        {
            "format": "edinet-archive-native-v1",
            "archives": [item["metadata"] for item in plan["archives"]],
            "acquisition_scope": plan["acquisition_scope"],
        },
    )


def _check_target(
    actual: FixedEdinetArchives, expected: FixedEdinetArchives, *, complete: bool
) -> None:
    if (
        not actual.archive_keys <= expected.archive_keys
        or actual.scope.join(expected.scope) != expected.scope
    ):
        raise ValueError("target contains acquisitions outside this finite import")
    for key in actual.archive_keys:
        if actual.metadata(*key) != expected.metadata(*key):
            raise ValueError("target archive metadata differs from the finite import")
        actual.read_archive(*key)
    if complete and (
        actual.archive_keys != expected.archive_keys or actual.scope != expected.scope
    ):
        raise ValueError("EDINET archive import readback is incomplete")


def apply_import(owner: EdinetArchiveL1, *, directory: Path) -> dict[str, Any]:
    plan = _plan(directory)
    expected = _expected(owner.store, plan)
    with _inputs(plan) as connections:
        current = owner.freeze()  # Explicit initialization; never recreate a lost head.
        _check_target(current, expected, complete=False)
        pending = owner.directory / "pending.json"
        if pending.exists():
            if json.loads(pending.read_bytes()) != {"create": False, "head": expected.value()}:
                raise ValueError("pending archive work belongs to another import")
            fixed = owner.resume_work()
        else:

            def originals() -> Iterator[NativeArchive]:
                for item in plan["archives"]:
                    metadata = ArchiveMetadata.model_validate_json(canonical_json(item["metadata"]))
                    row = (
                        connections[item["database"]]
                        .execute(
                            "SELECT zip_content FROM edinet_source_archives "
                            "WHERE doc_id=? AND source_type=?",
                            metadata.key,
                        )
                        .fetchone()
                    )
                    yield NativeArchive(metadata, row[0])

            fixed = owner.publish(originals(), scope=expected.scope)
        _check_target(fixed, expected, complete=True)
    result = {"archives": len(fixed.archive_keys), "l1_identity": fixed.identity}
    write_json_atomic(directory / "imported.json", result)
    return result


def verify_import(owner: EdinetArchiveL1, *, directory: Path) -> None:
    plan = _plan(directory)
    with _inputs(plan):
        _check_target(owner.freeze(), _expected(owner.store, plan), complete=True)
