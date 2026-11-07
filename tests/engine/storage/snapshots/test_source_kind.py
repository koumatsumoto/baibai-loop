from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pyarrow.parquet as pq
import pytest
from tests.helpers.lake_release import market_store

from baibai_engine.data_collection.providers.edinet.snapshot_contracts import EDINET_SOURCE_ARCHIVES
from baibai_engine.machine_analysis.observations.prices.snapshot_contracts import (
    JQUANTS_DAILY_BARS,
)
from baibai_engine.storage.snapshots.models import (
    DatasetManifest,
    L1DatasetSourceRef,
    NativeL1SourceRef,
    SourceRef,
    SQLiteSnapshotSourceRef,
    parquet_source_kind,
)
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import materialize_partitions
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

_SQLITE = SQLiteSnapshotSourceRef(
    kind="sqlite_snapshot",
    source_id=f"market-v1-{'a' * 24}",
    sha256="a" * 64,
    schema_version=1,
    captured_at=datetime(2026, 2, 1, tzinfo=UTC),
)
_L1 = L1DatasetSourceRef(
    kind="l1_dataset",
    source_id=f"l1-{'b' * 24}",
    sha256="b" * 64,
    dataset="example.source",
    build_id="input",
    key="lake/manifests/datasets/example.source/input.json",
)
_NATIVE = NativeL1SourceRef(
    kind="native_l1",
    source_id=f"native-l1-{'c' * 24}",
    sha256="c" * 64,
    namespace="example",
    key=f"fixed-inputs/example/v1/selections/{'c' * 64}.json",
)

_EDINET_NATIVE = NativeL1SourceRef(
    kind="native_l1",
    source_id=f"native-l1-{'c' * 24}",
    sha256="c" * 64,
    namespace="edinet",
    key=f"fixed-inputs/edinet/v1/selections/{'c' * 64}.json",
)


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        ((), "legacy_sqlite_import"),
        ((_SQLITE,), "legacy_sqlite_import"),
        ((_L1,), "l1_derived"),
        ((_NATIVE,), "l1_derived"),
        ((_EDINET_NATIVE,), "l1_derived"),
        ((_SQLITE, _L1), "l1_derived"),
        ((_NATIVE, _SQLITE), "l1_derived"),
        ((_L1, _NATIVE), "l1_derived"),
    ],
)
def test_parquet_source_kind_depends_on_lineage(
    sources: tuple[SourceRef, ...],
    expected: Literal["legacy_sqlite_import", "l1_derived"],
) -> None:
    assert parquet_source_kind(iter(sources)) == expected


@pytest.mark.parametrize("native_source", [None, _NATIVE, _EDINET_NATIVE])
def test_l2_writer_parity_and_reader_agree_on_source_kind(
    tmp_path: Path, native_source: NativeL1SourceRef | None
) -> None:
    source = market_store(tmp_path / "source.sqlite")
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=source, mirror_root=mirror) as snapshot:
        report = export_dataset_snapshot(
            dataset=JQUANTS_DAILY_BARS,
            source_snapshot=snapshot,
            native_source=native_source,
            native_coverage_status="complete" if native_source is not None else None,
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            build_id="output",
            created_at=datetime(2026, 2, 4, tzinfo=UTC),
        )
    paths = materialize_partitions(
        LakeObjectCache(root=tmp_path / "cache", source=LocalMirrorSource(mirror)),
        dataset=JQUANTS_DAILY_BARS,
        partitions=report.manifest.partitions,
    )
    expected = b"l1_derived" if native_source is not None else b"legacy_sqlite_import"
    assert paths
    for path in paths:
        assert pq.read_schema(path).metadata[b"baibai.source_kind"] == expected


@pytest.mark.parametrize(
    "sources", [(_NATIVE,), (_EDINET_NATIVE,), (_EDINET_NATIVE, _SQLITE), (_L1,)]
)
def test_raw_l1_rejects_native_or_derived_lineage(tmp_path, sources):
    source = market_store(tmp_path / "source.sqlite")
    with closing(open_connection(source)) as connection, connection:
        connection.execute(
            "INSERT INTO edinet_source_archives VALUES (?,?,?,?)",
            ("S0000001", 5, "2026-02-01", b"opaque provider bytes"),
        )
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=source, mirror_root=mirror) as snapshot:
        raw = export_dataset_snapshot(
            dataset=EDINET_SOURCE_ARCHIVES,
            source_snapshot=snapshot,
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            build_id="raw",
            created_at=datetime(2026, 2, 4, tzinfo=UTC),
        )
    payload = raw.manifest.model_dump(mode="json")
    for partition in payload["partitions"]:
        partition["sources"] = [ref.model_dump(mode="json") for ref in sources]
    with pytest.raises(ValueError, match="L1 source partitions"):
        DatasetManifest.model_validate_json(json.dumps(payload))
