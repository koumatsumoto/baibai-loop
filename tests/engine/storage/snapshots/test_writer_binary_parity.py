"""Verification batches may combine small archives, never many unbounded originals."""

import pyarrow as pa
import pyarrow.parquet as pq

from baibai_engine.storage.snapshots import writer


def test_binary_parity_reads_consecutive_small_groups_within_budget(tmp_path, monkeypatch):
    path = tmp_path / "archives.parquet"
    schema = pa.schema([("archive", pa.binary())])
    groups = [[bytes([n]) * 7] for n in range(9)]
    groups[2] = [b"medium-a" * 40]
    groups[3] = [b"medium-b" * 40]
    groups[4] = [b"large" * 4_000]
    groups[6] = [b"older-row-a", b"older-row-b"]
    with pq.ParquetWriter(path, schema, use_dictionary=False) as output:
        for group in groups:
            output.write_table(
                pa.Table.from_arrays([pa.array(group, type=pa.binary())], schema=schema)
            )
    with pq.ParquetFile(path, pre_buffer=False) as raw:
        cap = raw.metadata.row_group(2).total_byte_size + raw.metadata.row_group(0).total_byte_size
        monkeypatch.setattr(writer, "_BINARY_VERIFY_BYTES", cap)
        monkeypatch.setattr(writer, "_BINARY_VERIFY_GROUPS", 2)
        metadata = (raw.metadata.row_group(i) for i in range(raw.metadata.num_row_groups))
        calls = list(
            writer._binary_verify_batches((g.num_rows, g.total_byte_size) for g in metadata)
        )
        observed = [
            value
            for size, indices in calls
            for batch in raw.iter_batches(batch_size=size, row_groups=indices)
            for value in batch.column(0).to_pylist()
        ]
        assert observed == [value for group in groups for value in group]
        assert [i for _, indices in calls for i in indices] == list(range(len(groups)))
        assert (2, [0, 1]) in calls
        assert not any(2 in indices and 3 in indices for _, indices in calls)
        assert (1, [4]) in calls  # large individual original
        assert (1, [6]) in calls  # old multirow group has no per-row memory bound
        for size, indices in calls:
            if size > 1:
                assert len(indices) == size <= 2
                assert sum(raw.metadata.row_group(i).total_byte_size for i in indices) <= cap
