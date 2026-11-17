"""Object schema and row count come from one closed Parquet footer."""

import hashlib

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from baibai_engine.storage.snapshots.contracts import LakeColumn, LakeDataset
from baibai_engine.storage.snapshots.models import LakeObject
from baibai_engine.storage.snapshots.reader import LakeReadError, verify_object


@pytest.mark.parametrize("source_kind", ["legacy_sqlite_import", "l1_derived"])
@pytest.mark.parametrize("case", ["match", "empty", "metadata", "rows"])
def test_schema_and_count_share_one_closed_footer(tmp_path, monkeypatch, source_kind, case):
    dataset = LakeDataset(
        name="example.rows",
        sqlite_table="rows",
        date_column="day",
        columns=(LakeColumn("day", "TEXT", pa.string(), False, primary_key_ordinal=1),),
        layer="l2",
    )
    schema = dataset.arrow_schema_for(source_kind)
    if case == "metadata":
        schema = schema.with_metadata({**schema.metadata, b"baibai.dataset": b"other.rows"})
    rows = [] if case == "empty" else [{"day": "2026-11-17"}]
    path = tmp_path / "object.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    item = LakeObject(
        key=f"lake/l2/processed/example.rows/contract=v1/year=2026/month=11/part-{digest}.parquet",
        sha256=digest,
        bytes=len(payload),
        rows=2 if case == "rows" else len(rows),
        min_key=("2026-11-17",),
        max_key=("2026-11-17",),
    )
    parquet_file = pq.ParquetFile
    opened = []

    def observe(*args, **kwargs):
        file = parquet_file(*args, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(pq, "ParquetFile", observe)
    if case in {"metadata", "rows"}:
        message = "schema does not match" if case == "metadata" else "row count differs"
        with pytest.raises(LakeReadError, match=message):
            verify_object(path, dataset, item, source_kind=source_kind)
    else:
        verify_object(path, dataset, item, source_kind=source_kind)

    assert len(opened) == 1
    assert opened[0].closed
