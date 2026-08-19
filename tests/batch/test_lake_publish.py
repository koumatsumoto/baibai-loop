from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from baibai_batch.storage import lake_publish as lake_publish_module
from baibai_batch.storage.lake_publish import (
    AwsCliR2Store,
    LakeCASConflict,
    LakePublishError,
    RemoteObject,
    publish_l1_release,
)
from baibai_engine.market.lake import models as lake_models
from baibai_engine.market.lake.keys import current_calibration_bundle_pointer_key
from baibai_engine.market.lake.release import (
    L1ReleasePointer,
    canonical_lake_model_bytes,
    create_l1_release,
)
from baibai_engine.market.lake.writer import (
    export_lake_legacy,
    export_legacy_sqlite,
    sealed_sqlite_snapshot,
)
from baibai_engine.market.sqlite import open_connection
from baibai_engine.screening.calibration.lake import CalibrationBundlePointer


@dataclass
class _Value:
    body: bytes
    etag: str
    metadata: dict[str, str]
    content_type: str


class _MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, _Value] = {}
        self.conflict_pointer = False
        self.get_keys: list[str] = []
        self.head_keys: list[str] = []

    def head(self, key: str) -> RemoteObject | None:
        self.head_keys.append(key)
        value = self.values.get(key)
        if value is None:
            return None
        return RemoteObject(
            etag=value.etag,
            size=len(value.body),
            metadata=value.metadata,
            content_type=value.content_type,
        )

    def get_bytes(self, key: str) -> bytes:
        self.get_keys.append(key)
        return self.values[key].body

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
        self.get_keys.append(key)
        path.write_bytes(self.values[key].body)

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        content_md5: str,
        content_type: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> RemoteObject:
        current = self.values.get(key)
        if "/pointers/" in f"/{key}" and self.conflict_pointer:
            raise LakeCASConflict("injected")
        if if_none_match and current is not None:
            raise LakeCASConflict("exists")
        if if_match is not None and (current is None or current.etag != if_match):
            raise LakeCASConflict("stale")
        body = path.read_bytes()
        etag = hashlib.md5(body, usedforsecurity=False).hexdigest()  # nosec B324
        self.values[key] = _Value(
            body=body,
            etag=etag,
            metadata={
                "sha256": sha256,
                "content-md5": content_md5,
                "integrity": "content-md5-v1",
            },
            content_type=content_type,
        )
        result = self.head(key)
        assert result is not None
        return result


def _local_bundle(mirror: Path) -> CalibrationBundlePointer:
    return CalibrationBundlePointer.model_validate_json(
        (mirror / current_calibration_bundle_pointer_key()).read_bytes()
    )


@pytest.fixture(autouse=True)
def _fixed_publication_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lake_publish_module,
        "_utc_now",
        lambda: datetime(2026, 1, 7, tzinfo=UTC),
    )
    datasets = tuple(
        item.model_copy(
            update={
                "coverage_start_on_or_before": date.max,
                "minimum_rows": 1,
                "minimum_population_count": 1,
                "max_age_days": 10_000,
                "max_lead_days": 10_000,
            }
        )
        for item in lake_models.PRODUCTION_RELEASE_POLICY.datasets
        if item.dataset in {"jquants.daily_bars", "jquants.short_sale_reports"}
    )
    monkeypatch.setattr(
        lake_models,
        "PRODUCTION_RELEASE_POLICY",
        lake_models.PRODUCTION_RELEASE_POLICY.model_copy(update={"datasets": datasets}),
    )


def _add_short_sale_coverage(connection: sqlite3.Connection) -> None:
    connection.execute(
        """INSERT INTO source_coverage(
             source, coverage_key, coverage_start, coverage_end,
             fetched_at_utc, record_count, status, error
           ) VALUES ('jquants_short_sale_reports', 'test:pilot', '2026-01-01',
                     '2026-01-31', '2026-02-01T00:00:00+00:00', 1, 'ok', NULL)"""
    )


def _release(tmp_path: Path) -> tuple[Path, Path]:
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
    _add_short_sale_coverage(connection)
    connection.commit()
    connection.close()
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(
        sqlite_path=sqlite_path, mirror_root=mirror, snapshot_id="release-snapshot"
    ) as snapshot:
        datasets = [
            export_legacy_sqlite(
                dataset_name=name,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
                source_snapshot=snapshot,
                build_id=f"build-{index}",
                created_at=datetime(2026, 1, 7, tzinfo=UTC),
            ).manifest_path
            for index, name in enumerate(
                ("jquants.daily_bars", "jquants.short_sale_reports"), start=1
            )
        ]
    release_path, _ = create_l1_release(
        dataset_manifest_paths=datasets,
        mirror_root=mirror,
        release_id="release-1",
        created_at=datetime(2026, 1, 7, tzinfo=UTC),
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

    assert first.transfers.uploaded_objects == 5
    assert second.transfers.uploaded_objects == 0
    assert second.transfers.reused_objects == 5
    assert not any(key.startswith("lake/build-inputs/") for key in store.values)
    assert "lake/pointers/l1/current.json" in store.values
    # A run that changes nothing moves no object bytes. Objects the store already
    # holds are proved by the identity metadata their immutable write bound to them;
    # only the one mutable pointer is read back.
    assert second.transfers.downloaded_bytes == len(
        store.values["lake/pointers/l1/current.json"].body
    )
    assert not any(
        key.startswith("lake/l1/canonical/")
        for key in store.get_keys[len(store.get_keys) - second.transfers.get_requests :]
    )


def test_publication_proves_each_remote_key_once(tmp_path: Path) -> None:
    mirror, release_path = _release(tmp_path)
    store = _MemoryStore()
    publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
    store.head_keys.clear()

    publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)

    graph_heads = [key for key in store.head_keys if not key.startswith("lake/pointers/")]
    assert sorted(graph_heads) == sorted(set(graph_heads))
    assert set(graph_heads) == {key for key in store.values if not key.startswith("lake/pointers/")}


def test_verify_bytes_streams_the_whole_closure(tmp_path: Path) -> None:
    mirror, release_path = _release(tmp_path)
    store = _MemoryStore()
    publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)

    audited = publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=store,
        verify_bytes=True,
    )

    graph_bytes = sum(
        len(value.body)
        for key, value in store.values.items()
        if not key.startswith("lake/pointers/")
    )
    assert audited.transfers.downloaded_bytes > graph_bytes


def test_pointer_cas_conflict_leaves_current_unchanged(tmp_path) -> None:
    mirror, release_path = _release(tmp_path)
    store = _MemoryStore()
    publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
    original = store.values["lake/pointers/l1/current.json"].body
    successor_path, _ = create_l1_release(
        dataset_manifest_paths=sorted((mirror / "lake/manifests/datasets").glob("*/*.json")),
        mirror_root=mirror,
        release_id="release-2",
        created_at=datetime(2026, 1, 7, tzinfo=UTC),
    )
    store.conflict_pointer = True

    with pytest.raises(LakeCASConflict):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=successor_path,
            store=store,
        )

    assert store.values["lake/pointers/l1/current.json"].body == original


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


def test_publish_rejects_local_graph_symlinks_outside_mirror(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    metadata_link = mirror / "metadata.json"
    metadata_link.symlink_to(outside)

    with pytest.raises(LakePublishError, match="escapes mirror root"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=metadata_link,
            store=_MemoryStore(),
        )


def test_reuse_rejects_remote_integrity_metadata_change(tmp_path: Path) -> None:
    mirror, release_path = _release(tmp_path)
    store = _MemoryStore()
    publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=store,
    )
    key = next(key for key in store.values if key.endswith(".parquet"))
    value = store.values[key]
    value.metadata["sha256"] = "f" * 64

    with pytest.raises(LakePublishError, match="remote object metadata postcondition"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
        )


def test_release_dataset_digest_mismatch_never_creates_pointer(tmp_path: Path) -> None:
    mirror, release_path = _release(tmp_path)
    manifest_path = next((mirror / "lake/manifests/datasets").glob("*/*.json"))
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
    store = _MemoryStore()

    with pytest.raises(LakePublishError, match="release and dataset manifest disagree"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
        )

    assert "lake/pointers/l1/current.json" not in store.values


def test_same_release_id_requires_exact_pointer_identity(tmp_path: Path) -> None:
    mirror, release_path = _release(tmp_path)
    store = _MemoryStore()
    publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=store,
    )
    key = "lake/pointers/l1/current.json"
    wrong = canonical_lake_model_bytes(
        L1ReleasePointer(
            release_id="release-1",
            manifest_key="lake/manifests/releases/l1/release-1.json",
            manifest_sha256="f" * 64,
        )
    )
    store.values[key] = _Value(
        body=wrong,
        etag="wrong-pointer",
        metadata={
            "sha256": hashlib.sha256(wrong).hexdigest(),
            "content-md5": "transport-proof",
            "integrity": "content-md5-v1",
        },
        content_type="application/json",
    )

    with pytest.raises(LakePublishError, match="different identity"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
        )


def test_the_transfer_report_counts_every_request_the_run_made(tmp_path: Path) -> None:
    """The report exists to demonstrate differential cost, so it must not undercount.

    The adapter reads each object back after writing it, to prove the write landed. That
    read is a request this publication made; leaving it out of the figure that is used to
    judge request cost makes the figure argue for its own conclusion.
    """

    mirror, release_path = _release(tmp_path)
    store = _MemoryStore()

    report = publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)

    assert report.transfers.head_requests == len(store.head_keys)
    assert report.transfers.get_requests == len(store.get_keys)


def test_success_response_with_missing_remote_object_stops_before_pointer(
    tmp_path: Path,
) -> None:
    mirror, release_path = _release(tmp_path)

    class DroppingStore(_MemoryStore):
        def put_file(self, key: str, path: Path, **kwargs: object) -> RemoteObject:
            result = super().put_file(key, path, **kwargs)  # type: ignore[arg-type]
            self.values.pop(key)
            return result

    store = DroppingStore()
    with pytest.raises(LakePublishError, match="remote object is missing"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
        )
    assert "lake/pointers/l1/current.json" not in store.values


def test_verify_bytes_detects_replaced_remote_bytes_and_leaves_the_pointer(
    tmp_path: Path,
) -> None:
    mirror, release_path = _release(tmp_path)
    store = _MemoryStore()
    publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
    pointer = store.values["lake/pointers/l1/current.json"].body
    key = next(key for key in store.values if key.endswith(".parquet"))
    value = store.values[key]
    # Same length and same declared identity: only reading the bytes back can tell.
    value.body = bytes(len(value.body))

    publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
    with pytest.raises(LakePublishError, match="bytes postcondition"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=store,
            verify_bytes=True,
        )

    assert store.values["lake/pointers/l1/current.json"].body == pointer


def test_local_graph_mutation_after_validation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror, release_path = _release(tmp_path)
    original = lake_publish_module._ensure_immutable
    mutated = False

    def mutate_then_upload(*args: object, **kwargs: object) -> bool:
        nonlocal mutated
        path = kwargs["path"]
        assert isinstance(path, Path)
        if not mutated and path.suffix == ".parquet":
            path.write_bytes(path.read_bytes() + b"mutation")
            mutated = True
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(lake_publish_module, "_ensure_immutable", mutate_then_upload)
    with pytest.raises(LakePublishError, match="local upload source differs"):
        publish_l1_release(
            mirror_root=mirror,
            release_manifest_path=release_path,
            store=_MemoryStore(),
        )


def test_r2_adapter_classifies_409_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AwsCliR2Store(
        bucket="integration-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "not-logged",
            "R2_SECRET_ACCESS_KEY": "not-logged",
        },
    )
    conflict = subprocess.CompletedProcess(
        args=("aws",),
        returncode=1,
        stdout="",
        stderr="ConditionalRequestConflict (409)",
    )
    monkeypatch.setattr(lake_publish_module.subprocess, "run", lambda *args, **kwargs: conflict)
    with pytest.raises(LakeCASConflict):
        store._run("put-object", "--key", "test")

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired(cmd=("aws",), timeout=120)

    monkeypatch.setattr(lake_publish_module.subprocess, "run", timeout)
    with pytest.raises(LakePublishError, match="timed out"):
        store._run("put-object", "--key", "test")


def test_operation_timeout_covers_the_object_it_transfers() -> None:
    """A deadline shorter than the transfer turns a slow object into an ambiguous one."""

    two_gigabytes = 2_013_155_328
    measured_upload_seconds = 181

    assert lake_publish_module._operation_timeout(None) == 120
    assert lake_publish_module._operation_timeout(0) == 120
    assert lake_publish_module._operation_timeout(two_gigabytes) > measured_upload_seconds


def _two_month_release(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, Path]]:
    """A store whose history spans two months, published as one release."""

    sqlite_path = tmp_path / "market.sqlite"
    connection = open_connection(sqlite_path)
    connection.executemany(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
        [("1301", "2026-01-28", 1.0), ("1301", "2026-02-04", 2.0)],
    )
    connection.executemany(
        """INSERT INTO jquants_short_sale_reports(
             disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name,
             discretionary_investment_contractor_name, investment_fund_name, is_cancellation
           ) VALUES (?, 0, ?, '7203', 'Fund', '', '', 0)""",
        [("2026-01-29", "2026-01-28"), ("2026-02-05", "2026-02-04")],
    )
    connection.execute(
        """INSERT INTO source_coverage(
             source, coverage_key, coverage_start, coverage_end,
             fetched_at_utc, record_count, status, error
           ) VALUES ('jquants_short_sale_reports', 'test:pilot', '2026-01-01',
                     '2026-02-28', '2026-03-01T00:00:00+00:00', 2, 'ok', NULL)"""
    )
    connection.commit()
    connection.close()
    mirror = tmp_path / "mirror"
    build = export_lake_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    bases = {name: item.manifest_path for name, item in build.datasets.items()}
    release_path, _ = create_l1_release(
        dataset_manifest_paths=sorted(bases.values()),
        mirror_root=mirror,
        release_id="two-month-1",
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    return sqlite_path, mirror, release_path, bases


def test_a_one_month_correction_moves_only_that_month(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The differential claim, end to end: one changed month, one month of transfer."""

    monkeypatch.setattr(lake_publish_module, "_utc_now", lambda: datetime(2026, 3, 2, tzinfo=UTC))
    sqlite_path, mirror, release_path, bases = _two_month_release(tmp_path)
    store = _MemoryStore()
    first = publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
    closure_bytes = sum(
        len(value.body)
        for key, value in store.values.items()
        if not key.startswith("lake/pointers/")
    )

    connection = open_connection(sqlite_path)
    connection.execute("UPDATE jquants_daily_bars SET close = 9.0 WHERE traded_at = '2026-02-04'")
    connection.commit()
    connection.close()
    corrected = export_lake_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        base_manifest_paths=bases,
        created_at=datetime(2026, 3, 2, tzinfo=UTC),
    )
    successor_path, _ = create_l1_release(
        dataset_manifest_paths=sorted(item.manifest_path for item in corrected.datasets.values()),
        mirror_root=mirror,
        release_id="two-month-2",
        created_at=datetime(2026, 3, 2, tzinfo=UTC),
    )

    before = len(store.get_keys)
    second = publish_l1_release(
        mirror_root=mirror, release_manifest_path=successor_path, store=store
    )

    assert corrected.datasets["jquants.daily_bars"].changed_partitions == ("2026-02",)
    # One rewritten Parquet object plus the manifests that name it — not the history.
    assert second.transfers.uploaded_objects == 4
    assert second.transfers.uploaded_bytes < first.transfers.uploaded_bytes
    # Only the month this run rewrote is streamed back. The history's Parquet objects
    # are proved from the identity their immutable write bound to them.
    read_back = [key for key in store.get_keys[before:] if key.endswith(".parquet")]
    assert len(read_back) == 1
    assert second.transfers.downloaded_bytes < closure_bytes
