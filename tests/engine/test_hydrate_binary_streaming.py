"""Large retained binary rows must not require a whole-object SQL sort."""

import sqlite3
from contextlib import closing

import pyarrow as pa
import pyarrow.parquet as pq

from baibai_engine.data_collection.providers.edinet.snapshot_contracts import (
    EDINET_SOURCE_ARCHIVES,
)
from baibai_engine.data_collection.providers.edinet.storage_contracts import (
    EDINET_SOURCE_ARCHIVES_DDL,
)
from baibai_engine.storage.snapshots.hydrate import _binary_hydrate_batches


def test_binary_hydrate_streams_one_row_group_at_a_time_and_keeps_exact_bytes(tmp_path):
    dataset = EDINET_SOURCE_ARCHIVES
    names = tuple(column.name for column in dataset.columns)
    rows = [
        (f"SYNTHETIC-{index:03d}", 1, "2026-10-30", bytes([index]) * 131_072)
        for index in reversed(range(17))
    ]
    path = tmp_path / "source-archives.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [dict(zip(names, row, strict=True)) for row in rows],
            schema=dataset.arrow_schema,
        ),
        path,
        row_group_size=1,
    )
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(EDINET_SOURCE_ARCHIVES_DDL)
        batches = 0
        emitted_doc_ids = []
        for batch in _binary_hydrate_batches((path,), names):
            assert len(batch) == 1
            emitted_doc_ids.extend(row[0] for row in batch)
            connection.executemany("INSERT INTO edinet_source_archives VALUES (?, ?, ?, ?)", batch)
            batches += 1
        assert batches == len(rows)
        assert emitted_doc_ids == [row[0] for row in rows]
        assert connection.execute(
            "SELECT doc_id, source_type, stored_on, zip_content "
            "FROM edinet_source_archives ORDER BY doc_id"
        ).fetchall() == sorted(rows)
