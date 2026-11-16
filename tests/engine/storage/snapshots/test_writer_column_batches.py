"""Column construction must preserve the previous row-dict Parquet bytes."""

from itertools import islice

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from baibai_engine.storage.snapshots import writer
from baibai_engine.storage.snapshots.contracts import (
    PROJECTION_REVISION_METADATA_KEY,
    LakeColumn,
    LakeDataset,
)


@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.parametrize("revision", [None, "fixed-projection"])
def test_column_batches_keep_exact_row_writer_bytes(tmp_path, monkeypatch, binary, revision):
    columns = (
        LakeColumn("day", "TEXT", pa.string(), False, 1),
        LakeColumn("id", "INTEGER", pa.int64(), False, 2),
        LakeColumn("name", "TEXT", pa.string(), True),
        LakeColumn("value", "REAL", pa.float64(), True),
    )
    rows = [
        ("2026-11-16", 1, "日本語", None),
        ("2026-11-16", 2, None, -0.0),
        ("2026-11-16", 3, "", 0.0),
        ("2026-11-16", 4, "a", 1.234567890123),
        ("2026-11-16", 5, "b", -123.4),
    ]
    if binary:
        columns += (LakeColumn("body", "BLOB", pa.binary(), True),)
        rows = [
            (*row, payload)
            for row, payload in zip(rows, [b"\x00\xff", None, b"", b"x", b"y"], strict=True)
        ]
    dataset = LakeDataset(
        name="test.column_batch",
        sqlite_table="column_batch",
        date_column="day",
        columns=columns,
        layer="l1_source",
    )
    monkeypatch.setattr(writer, "_ROW_GROUP_SIZE", 2)
    schema = dataset.arrow_schema_for("legacy_sqlite_import")
    size = writer._row_batch_size(dataset)
    expected = tmp_path / "row-reference.parquet"
    with pq.ParquetWriter(
        expected,
        schema,
        compression=writer._COMPRESSION,
        compression_level=writer._COMPRESSION_LEVEL,
        data_page_version="1.0",
        use_dictionary=False,
        version=writer._PARQUET_VERSION,
        write_statistics=[column.name for column in columns if column.sqlite_type != "BLOB"],
    ) as reference:
        if revision is not None:
            reference.add_key_value_metadata({PROJECTION_REVISION_METADATA_KEY: revision.encode()})
        iterator = iter(rows)
        while batch := list(islice(iterator, size)):
            reference.write_table(
                pa.Table.from_pylist(
                    [dict(zip(schema.names, row, strict=True)) for row in batch], schema=schema
                ),
                row_group_size=size,
            )
    built = writer._build_period(
        dataset=dataset,
        period=(2026, 11),
        staging_root=tmp_path,
        rows=iter(rows),
        sources=(),
        projection_revision=revision,
    )
    assert built.staged_path.read_bytes() == expected.read_bytes()
    assert built.manifest.objects[0].rows == len(rows)
    writer._validate_parquet(
        built.staged_path,
        dataset=dataset,
        period=(2026, 11),
        expected_rows=iter(rows),
        expected_object=built.manifest.objects[0],
    )
    assert pq.ParquetFile(built.staged_path).num_row_groups == (5 if binary else 3)


@pytest.mark.parametrize(
    "rows", [[("2026-11-16",)], [("2026-11-16", 1), ("2026-11-16",)], [("2026-11-16", 1, "extra")]]
)
def test_wrong_row_width_still_refuses(tmp_path, rows):
    dataset = LakeDataset(
        name="test.bad_width",
        sqlite_table="bad_width",
        date_column="day",
        layer="l1_source",
        columns=(
            LakeColumn("day", "TEXT", pa.string(), False, 1),
            LakeColumn("id", "INTEGER", pa.int64(), False, 2),
        ),
    )
    with pytest.raises(ValueError, match="zip"):
        writer._build_period(
            dataset=dataset, period=(2026, 11), staging_root=tmp_path, rows=rows, sources=()
        )


@pytest.mark.parametrize(
    "failure", ["value", "order", "short", "extra", "duplicate", "period", "count", "bounds"]
)
def test_column_validation_keeps_rejections(tmp_path, monkeypatch, failure):
    dataset = LakeDataset(
        name="test.validation",
        sqlite_table="validation",
        date_column="day",
        layer="l1_source",
        columns=(
            LakeColumn("day", "TEXT", pa.string(), False, 1),
            LakeColumn("id", "INTEGER", pa.int64(), False, 2),
            LakeColumn("value", "REAL", pa.float64(), True),
        ),
    )
    monkeypatch.setattr(writer, "_ROW_GROUP_SIZE", 2)
    rows = [("2026-11-16", i, None if i == 1 else float(i)) for i in range(4)]
    if failure == "duplicate":
        rows[2] = rows[1]  # duplicate across batches
    if failure == "period":
        rows[2] = ("2026-12-16", 2, 2.0)
    built = writer._build_period(
        dataset=dataset, period=(2026, 11), staging_root=tmp_path, rows=rows, sources=()
    )
    expected = rows.copy()
    obj = built.manifest.objects[0]
    if failure == "value":
        expected[1] = ("2026-11-16", 1, 1.0)
    elif failure == "order":
        expected.reverse()
    elif failure == "short":
        expected.pop()
    elif failure == "extra":
        expected.append(("2026-11-16", 4, 4.0))
    elif failure == "count":
        obj = obj.model_copy(update={"rows": 5})
    elif failure == "bounds":
        obj = obj.model_copy(update={"max_key": ("2026-11-16", "4")})
    message = {
        "duplicate": "not unique",
        "period": "escapes",
        "count": "count",
        "bounds": "min/max",
    }.get(failure, "values or row order")
    with pytest.raises(writer.LakeBuildError, match=message):
        writer._validate_parquet(
            built.staged_path,
            dataset=dataset,
            period=(2026, 11),
            expected_rows=iter(expected),
            expected_object=obj,
        )
