from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from baibai_batch.storage.lake_publish import (
    AwsCliR2Store,
    LakeCASConflict,
    LakePublishError,
    RemoteObject,
    publish_l1_release,
    publish_raw_archive,
)
from baibai_engine.market.lake.raw import RawArchiveMetadata, RawRetentionClass, archive_raw_file
from baibai_engine.market.lake.release import (
    L1ReleasePointer,
    canonical_json_bytes,
    create_l1_release,
)
from baibai_engine.market.lake.writer import export_legacy_sqlite
from baibai_engine.market.sqlite import open_connection


@dataclass
class _Value:
    body: bytes
    etag: str
    metadata: dict[str, str]


class _MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, _Value] = {}
        self.conflict_pointer = False

    def head(self, key: str) -> RemoteObject | None:
        value = self.values.get(key)
        if value is None:
            return None
        return RemoteObject(etag=value.etag, size=len(value.body), metadata=value.metadata)

    def get_bytes(self, key: str) -> bytes:
        return self.values[key].body

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        content_type: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> RemoteObject:
        del content_type
        current = self.values.get(key)
        if key.endswith("pointers/l1/current.json") and self.conflict_pointer:
            raise LakeCASConflict("injected")
        if if_none_match and current is not None:
            raise LakeCASConflict("exists")
        if if_match is not None and (current is None or current.etag != if_match):
            raise LakeCASConflict("stale")
        body = path.read_bytes()
        etag = hashlib.md5(body, usedforsecurity=False).hexdigest()  # nosec B324
        self.values[key] = _Value(body=body, etag=etag, metadata={"sha256": sha256})
        result = self.head(key)
        assert result is not None
        return result


def _release(tmp_path: Path) -> tuple[Path, Path]:
    sqlite_path = tmp_path / "market.sqlite"
    connection = open_connection(sqlite_path)
    connection.execute(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES ('1301', '2026-01-05', 1)"
    )
    connection.commit()
    connection.close()
    mirror = tmp_path / "mirror"
    dataset = export_legacy_sqlite(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        build_id="build-1",
        created_at=datetime(2026, 1, 6, tzinfo=UTC),
    )
    release_path, _ = create_l1_release(
        dataset_manifest_paths=[dataset.manifest_path],
        mirror_root=mirror,
        release_id="release-1",
        created_at=datetime(2026, 1, 6, tzinfo=UTC),
    )
    return mirror, release_path


def test_publish_uploads_immutable_graph_before_current_pointer(tmp_path) -> None:
    mirror, release_path = _release(tmp_path)
    store = _MemoryStore()

    first = publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=store,
    )
    second = publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=store,
    )

    assert first.uploaded_objects == 3
    assert second.uploaded_objects == 0
    assert second.reused_objects == 3
    assert "lake/pointers/l1/current.json" in store.values


def test_pointer_cas_conflict_leaves_current_unchanged(tmp_path) -> None:
    mirror, release_path = _release(tmp_path)
    store = _MemoryStore()
    original = canonical_json_bytes(
        L1ReleasePointer(
            release_id="release-previous",
            manifest_key="lake/manifests/releases/l1/release-previous.json",
            manifest_sha256="b" * 64,
        )
    )
    store.values["lake/pointers/l1/current.json"] = _Value(
        body=original,
        etag="old-etag",
        metadata={"sha256": hashlib.sha256(original).hexdigest()},
    )
    store.conflict_pointer = True

    with pytest.raises(LakeCASConflict):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
        )

    assert store.values["lake/pointers/l1/current.json"].body == original


def test_raw_object_and_metadata_publish_idempotently(tmp_path) -> None:
    source = tmp_path / "response.json.gz"
    source.write_bytes(b"provider-original")
    mirror = tmp_path / "mirror"
    _, metadata_path, _ = archive_raw_file(
        source_path=source,
        mirror_root=mirror,
        provider="jquants",
        dataset="jquants.daily_bars",
        ingest_id="raw-1",
        suffix=".json.gz",
        retention_class=RawRetentionClass.PRESERVE,
        retrieved_at=datetime(2026, 1, 6, tzinfo=UTC),
    )
    store = _MemoryStore()

    first = publish_raw_archive(
        mirror_root=mirror,
        metadata_path=metadata_path,
        store=store,
    )
    second = publish_raw_archive(
        mirror_root=mirror,
        metadata_path=metadata_path,
        store=store,
    )

    assert first.uploaded_objects == 2
    assert second.uploaded_objects == 0
    assert second.reused_objects == 2


def test_r2_account_id_rejects_endpoint_injection() -> None:
    with pytest.raises(LakePublishError, match="32 lowercase hexadecimal"):
        AwsCliR2Store(
            bucket="baibai-stores",
            env={
                "R2_ACCOUNT_ID": "safe@attacker.example/",
                "R2_ACCESS_KEY_ID": "not-logged",
                "R2_SECRET_ACCESS_KEY": "not-logged",
            },
        )


def test_raw_metadata_rejects_a_non_raw_namespace(tmp_path) -> None:
    source = tmp_path / "response.json.gz"
    source.write_bytes(b"provider-original")
    _, _, metadata = archive_raw_file(
        source_path=source,
        mirror_root=tmp_path / "mirror",
        provider="jquants",
        dataset="jquants.daily_bars",
        ingest_id="raw-key",
        suffix=".json.gz",
        retention_class=RawRetentionClass.PRESERVE,
        retrieved_at=datetime(2026, 1, 6, tzinfo=UTC),
    )
    payload = metadata.model_dump(mode="json")
    payload["object_key"] = "lake/pointers/l1/current.json"

    with pytest.raises(ValueError, match="object_key"):
        RawArchiveMetadata.model_validate(payload)


def test_release_publish_closes_referenced_raw_graph(tmp_path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    connection = open_connection(sqlite_path)
    connection.execute(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES ('1301', '2026-01-05', 1)"
    )
    connection.commit()
    connection.close()
    mirror = tmp_path / "mirror"
    source = tmp_path / "response.json.gz"
    source.write_bytes(b"provider-original")
    archive_raw_file(
        source_path=source,
        mirror_root=mirror,
        provider="jquants",
        dataset="jquants.daily_bars",
        ingest_id="raw-release",
        suffix=".json.gz",
        retention_class=RawRetentionClass.PRESERVE,
        retrieved_at=datetime(2026, 1, 6, tzinfo=UTC),
    )
    dataset = export_legacy_sqlite(
        dataset_name="jquants.daily_bars",
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        source_ingest_ids=("raw-release",),
        build_id="raw-build",
    )
    release_path, _ = create_l1_release(
        dataset_manifest_paths=[dataset.manifest_path],
        mirror_root=mirror,
        release_id="raw-release-1",
    )
    store = _MemoryStore()

    report = publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=store,
    )

    assert report.uploaded_objects == 5


def test_release_allows_the_same_ingest_id_in_two_dataset_namespaces(tmp_path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    connection = open_connection(sqlite_path)
    connection.execute(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES ('1301', '2026-01-05', 1)"
    )
    connection.execute(
        """INSERT INTO jquants_short_sale_reports(
             disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name,
             discretionary_investment_contractor_name, investment_fund_name, is_cancellation
           ) VALUES ('2026-01-06', 0, '2026-01-05', '7203', 'Fund', '', '', 0)"""
    )
    connection.commit()
    connection.close()
    mirror = tmp_path / "mirror"
    manifests: list[Path] = []
    for index, dataset in enumerate(("jquants.daily_bars", "jquants.short_sale_reports"), start=1):
        source = tmp_path / f"response-{index}.json.gz"
        source.write_bytes(f"provider-original-{index}".encode())
        archive_raw_file(
            source_path=source,
            mirror_root=mirror,
            provider="jquants",
            dataset=dataset,
            ingest_id="shared-run",
            suffix=".json.gz",
            retention_class=RawRetentionClass.PRESERVE,
            retrieved_at=datetime(2026, 1, 6, tzinfo=UTC),
        )
        manifests.append(
            export_legacy_sqlite(
                dataset_name=dataset,
                sqlite_path=sqlite_path,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
                source_ingest_ids=("shared-run",),
                build_id=f"shared-build-{index}",
            ).manifest_path
        )
    release_path, _ = create_l1_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id="shared-release",
    )

    report = publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=_MemoryStore(),
    )

    assert report.uploaded_objects == 9
