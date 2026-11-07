"""固定EDINET原本を既存SQL consumerへ渡す。取得計画には使わない。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from baibai_engine.data_collection.providers.edinet.archive_native import (
    EdinetArchiveL1,
    FixedEdinetArchives,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.facts_scope import (
    FactsScope,
    merge_scope,
    read_scope,
)
from baibai_engine.data_collection.providers.edinet.list_native import FixedEdinetLists
from baibai_engine.storage.filesystem import fsync_directory
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.edinet_list_native import materialize_lists


def export_pending_handoff(
    owner: EdinetArchiveL1, *, source: str, asof: date, started_at: datetime
) -> bool:
    """Keep failed native publication in the existing private receipt handoff."""
    pending_path = owner.directory / "pending.json"
    if not pending_path.exists():
        return False
    pending = json.loads(pending_path.read_bytes())
    if source not in {"edinet-metrics", "edinet-facts"} or pending.get("create") is not False:
        raise ValueError("invalid EDINET archive acquisition work")
    fixed = FixedEdinetArchives(owner.store, pending["head"])
    output = owner.directory / "receipts.sqlite"
    if output.exists():
        raise FileExistsError("EDINET pending archive handoff already exists")
    with closing(open_connection(output)) as connection, connection:
        for doc_id, source_type in sorted(fixed.archive_keys):
            metadata = fixed.metadata(doc_id, source_type)
            assert metadata is not None
            item = NativeArchive(
                metadata, (owner.directory / (metadata.sha256 + ".object")).read_bytes()
            )
            connection.execute(
                "INSERT INTO edinet_source_archives VALUES (?,?,?,?)",
                (doc_id, source_type, metadata.stored_on.isoformat(), item.content),
            )
        merge_scope(connection, fixed.scope)
    write_json_atomic(
        owner.directory / "result.json",
        {
            "source": source,
            "asof": asof.isoformat(),
            "started_at": started_at.isoformat(),
            "list_receipts": [],
            "archives": sorted(fixed.archive_keys),
            "native_archive_head": fixed.value(),
            "native_l1_pending": True,
            "acquisition": {"status": "failed"},
        },
    )
    return True


def project_archive_scope(
    archives: FixedEdinetArchives, connection: sqlite3.Connection
) -> FactsScope:
    """Join requested scope in the caller's transaction without reading ZIP bodies.

    Requests may have no received archive. Projection does not create archive
    inventory, receipt clocks or extraction success, and never commits the copy.
    """
    return merge_scope(connection, archives.scope)


def materialize_archives(archives: FixedEdinetArchives, database: Path) -> dict[str, object]:
    """Reuse verified execution-copy bytes; fetch only missing fixed originals.

    An older execution copy can hold a later receipt day than the fixed head.
    Reconcile that day only in this disposable copy, never in L1.
    Reject originals or scope that are outside the fixed head.
    """
    retained = set()
    adjusted = []
    with closing(open_connection(database)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if read_scope(connection).join(archives.scope) != archives.scope:
            raise ValueError("EDINET cache scope is outside the fixed native head")
        for doc_id, source_type, stored_on, body in connection.execute(
            "SELECT doc_id,source_type,stored_on,zip_content FROM edinet_source_archives"
        ):
            metadata = archives.metadata(doc_id, source_type)
            recorded = date.fromisoformat(stored_on)
            if (
                metadata is None
                or recorded.isoformat() != stored_on
                or hashlib.sha256(body).hexdigest() != metadata.sha256
                or len(body) != metadata.size
                or recorded < metadata.stored_on
            ):
                raise ValueError("EDINET cache original differs from the fixed native head")
            retained.add((doc_id, source_type))
            if stored_on != metadata.stored_on.isoformat():
                adjusted.append((metadata.stored_on.isoformat(), doc_id, source_type))
        connection.executemany(
            "UPDATE edinet_source_archives SET stored_on=? WHERE doc_id=? AND source_type=?",
            adjusted,
        )
        for doc_id, source_type in sorted(archives.archive_keys - retained):
            metadata = archives.metadata(doc_id, source_type)
            assert metadata is not None
            connection.execute(
                "INSERT INTO edinet_source_archives VALUES (?,?,?,?)",
                (
                    doc_id,
                    source_type,
                    metadata.stored_on.isoformat(),
                    archives.read_archive(doc_id, source_type),
                ),
            )
        project_archive_scope(archives, connection)
    return {
        "exit_code": 0,
        "status": "completed",
        "l1_identity": archives.identity,
        "archive_count": len(archives.archive_keys),
        "copied_archives": len(archives.archive_keys - retained),
    }


def rebuild_fixed_edinet_input(
    lists: FixedEdinetLists, archives: FixedEdinetArchives, output: Path
) -> dict[str, object]:
    """Replay only the supplied heads; never overwrite or extend an older market.

    This explicit cold adapter reads all selected archive bodies. Acquisition
    planning must continue using the metadata inventory, not this materialization.
    """
    if output.exists() or output.is_symlink():
        raise FileExistsError("EDINET fixed input requires a new output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="edinet-read-", dir=output.parent) as temporary:
        database = Path(temporary) / "source.sqlite"
        list_report = materialize_lists(lists, database)
        materialize_archives(archives, database)
        # 完成後だけ公開する。exclusive linkで遅れて作られた既存出力も上書きしない。
        os.link(database, output)
        fsync_directory(output.parent)
    return {
        "status": list_report["status"],
        "list_identity": lists.identity,
        "archive_identity": archives.identity,
        "list_failed_days": list_report["failed_days"],
        "archive_count": len(archives.archive_keys),
    }
