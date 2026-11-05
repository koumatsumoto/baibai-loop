"""固定EDINET原本を、既存SQL consumer用の新規使い捨てcacheへ渡す。"""

from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
from baibai_engine.data_collection.providers.edinet.facts_scope import merge_scope
from baibai_engine.data_collection.providers.edinet.list_native import FixedEdinetLists
from baibai_engine.storage.filesystem import fsync_directory
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.edinet_list_native import materialize_lists


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
        with closing(open_connection(database)) as connection:
            for doc_id, source_type in sorted(archives.archive_keys):
                metadata = archives.metadata(doc_id, source_type)
                assert metadata is not None
                retain_source_archive(
                    connection,
                    doc_id,
                    source_type,
                    archives.read_archive(doc_id, source_type),
                    stored_on=metadata.stored_on,
                )
            with connection:
                merge_scope(connection, archives.scope)
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
