from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from baibai_engine.market.lake import duck as duck_module
from baibai_engine.market.lake import models as lake_models
from baibai_engine.market.lake import projection as projection_module
from baibai_engine.market.lake.datasets import JQUANTS_DAILY_BARS, JQUANTS_SHORT_SALE_REPORTS
from baibai_engine.market.lake.duck import (
    LakeCredentialError,
    LakeSession,
    R2ReadCredentials,
    lake_session,
)
from baibai_engine.market.lake.keys import (
    canonical_object_key,
    current_l1_pointer_key,
    release_manifest_key,
)
from baibai_engine.market.lake.models import DatasetManifest, L1ReleaseSourceRef, LakeObject
from baibai_engine.market.lake.objects import (
    LakeObjectCache,
    LakeObjectError,
    LocalMirrorSource,
    mirror_path,
    mirror_root,
    sha256_file,
)
from baibai_engine.market.lake.projection import (
    ProjectionError,
    build_projection,
    projection_fingerprint,
    read_projection_identity,
)
from baibai_engine.market.lake.reader import (
    LakeReadError,
    accepted_dataset,
    iter_partition_rows,
    resolve_previous_release,
    resolve_release,
    resolve_release_ref,
    selected_partitions,
    verify_object,
)
from baibai_engine.market.lake.reader import (
    resolve_current_release as _resolve_current_release_at,
)
from baibai_engine.market.lake.release import (
    L1ReleasePointer,
    canonical_json_bytes,
    create_l1_release,
)
from baibai_engine.market.lake.retention import LakeRetentionError, exclusive_lock
from baibai_engine.market.lake.writer import capture_legacy_sqlite_snapshot, export_legacy_sqlite
from baibai_engine.market.sqlite import open_connection

_COMMIT = "b" * 40
_OTHER_COMMIT = "c" * 40
_BUILT_AT = datetime(2026, 8, 12, 3, 0, tzinfo=UTC)


def resolve_current_release(source: object):
    return _resolve_current_release_at(source, evaluated_at=_BUILT_AT)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _small_pilot_release_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    datasets = tuple(
        item.model_copy(
            update={
                "coverage_start_on_or_before": date.max,
                "minimum_rows": 1,
                "minimum_population_count": 1,
            }
        )
        for item in lake_models.PILOT_RELEASE_POLICY.datasets
    )
    monkeypatch.setattr(
        lake_models,
        "PILOT_RELEASE_POLICY",
        lake_models.PILOT_RELEASE_POLICY.model_copy(
            update={"datasets": datasets, "max_dataset_age_days": 366}
        ),
    )


@dataclass
class RecordingSource:
    """A local mirror that remembers every key a run asked for."""

    inner: LocalMirrorSource
    reads: list[str] = field(default_factory=list)

    def read_bytes(self, key: str) -> bytes:
        self.reads.append(key)
        return self.inner.read_bytes(key)

    def uri(self, key: str) -> str:
        return self.inner.uri(key)


@dataclass(frozen=True)
class Lake:
    root: Path
    mirror: Path
    sqlite_path: Path
    release_id: str


def _market_store(path: Path) -> Path:
    connection = open_connection(path)
    connection.executemany(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close, volume) VALUES (?, ?, ?, ?)",
        [
            ("1301", "2026-01-05", 100.0, 1000.0),
            ("7203", "2026-01-05", 200.0, 2000.0),
            ("1301", "2026-01-20", 105.0, 1100.0),
            ("1301", "2026-02-02", 110.0, 1200.0),
        ],
    )
    connection.execute(
        """INSERT INTO source_coverage(
             source, coverage_key, coverage_start, coverage_end,
             fetched_at_utc, record_count, status, error
           ) VALUES ('jquants_short_sale_reports', 'test:pilot', '2026-01-01',
                     '2026-02-28', '2026-03-01T00:00:00+00:00', 2, 'ok', NULL)"""
    )
    connection.executemany(
        """INSERT INTO jquants_short_sale_reports(
             disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name,
             discretionary_investment_contractor_name, investment_fund_name,
             short_ratio, short_shares, short_trading_units, is_cancellation
           ) VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?)""",
        [
            ("2026-01-06", 0, "2026-01-05", "7203", "Fund A", 0.006, 600, 6, 0),
            ("2026-02-03", 0, "2026-02-02", "6758", "Fund B", None, None, None, 1),
        ],
    )
    connection.commit()
    connection.close()
    return path


def _publish_pointer(mirror: Path, release_id: str, manifest_path: Path) -> None:
    target = mirror / current_l1_pointer_key()
    previous = (
        L1ReleasePointer.model_validate_json(target.read_bytes()) if target.is_file() else None
    )
    pointer = L1ReleasePointer(
        release_id=release_id,
        manifest_key=manifest_path.relative_to(mirror).as_posix(),
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        previous_release_id=None if previous is None else previous.release_id,
        previous_manifest_sha256=None if previous is None else previous.manifest_sha256,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(canonical_json_bytes(pointer))


def _release_digest(mirror: Path, release_id: str) -> str:
    path = mirror / release_manifest_key(release_id=release_id)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_lake(root: Path, *, release_id: str = "release-one") -> Lake:
    sqlite_path = _market_store(root / "market.sqlite")
    mirror = root / "mirror"
    snapshot = capture_legacy_sqlite_snapshot(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        snapshot_id=f"snapshot-{release_id}",
    )
    manifests = [
        export_legacy_sqlite(
            dataset_name=name,
            sqlite_path=sqlite_path,
            mirror_root=mirror,
            producer_git_commit=_COMMIT,
            source_snapshot_ref=snapshot.ref,
            build_id=f"build-{name.replace('.', '-')}-{release_id}",
            created_at=_BUILT_AT,
        ).manifest_path
        for name in ("jquants.daily_bars", "jquants.short_sale_reports")
    ]
    manifest_path, _release = create_l1_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id=release_id,
        created_at=_BUILT_AT,
    )
    _publish_pointer(mirror, release_id, manifest_path)
    return Lake(root=root, mirror=mirror, sqlite_path=sqlite_path, release_id=release_id)


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    return _build_lake(tmp_path)


@pytest.fixture
def session() -> Iterator[LakeSession]:
    with lake_session() as opened:
        yield opened


def replace_manifest_contract(
    manifest: DatasetManifest, *, contract_version: int
) -> DatasetManifest:
    """Re-key one manifest onto another contract version, keeping it self-consistent."""

    partitions = tuple(
        partition.model_copy(
            update={
                "objects": tuple(
                    item.model_copy(
                        update={
                            "key": canonical_object_key(
                                layer=manifest.layer,
                                dataset=manifest.dataset,
                                contract_version=contract_version,
                                partition_values=partition.values,
                                partition_by=manifest.partition_by,
                                content_sha256=item.sha256,
                            )
                        }
                    )
                    for item in partition.objects
                )
            }
        )
        for partition in manifest.partitions
    )
    return manifest.model_copy(
        update={"contract_version": contract_version, "partitions": partitions}
    )


def _table_shape(
    connection: sqlite3.Connection, table: str
) -> tuple[list[tuple[object, ...]], list[tuple[str, tuple[str, ...]]]]:
    """Columns and index definitions of one table, as structure rather than text."""

    columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
    indexes: list[tuple[str, tuple[str, ...]]] = []
    for row in connection.execute(f"PRAGMA index_list({table})").fetchall():
        name = str(row[1])
        if name.startswith("sqlite_autoindex_"):
            continue
        members = tuple(
            str(item[2]) for item in connection.execute(f"PRAGMA index_info({name})").fetchall()
        )
        indexes.append((name, members))
    return columns, sorted(indexes)


def _cache(lake: Lake, *, recording: RecordingSource | None = None) -> LakeObjectCache:
    source = recording or LocalMirrorSource(lake.mirror)
    return LakeObjectCache(root=lake.mirror, source=source)


def _build(
    session: LakeSession,
    lake: Lake,
    *,
    destination: Path,
    cache: LakeObjectCache | None = None,
    commit: str = _COMMIT,
    force: bool = False,
):
    used = cache or _cache(lake)
    release = resolve_current_release(used.source)
    return build_projection(
        session,
        release=release,
        cache=used,
        destination=destination,
        dataset_names=("jquants.daily_bars", "jquants.short_sale_reports"),
        builder_git_commit=commit,
        force=force,
        built_at=_BUILT_AT,
    )


class TestFixedRelease:
    def test_pointer_is_read_once_and_a_mid_run_switch_does_not_change_the_input(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        recording = RecordingSource(LocalMirrorSource(lake.mirror))
        cache = _cache(lake, recording=recording)
        release = resolve_current_release(cache.source)
        # A publisher switches the pointer while the run is still going.
        newer = _build_lake(tmp_path / "second", release_id="release-two")
        (lake.mirror / current_l1_pointer_key()).write_bytes(
            (newer.mirror / current_l1_pointer_key()).read_bytes()
        )

        report = build_projection(
            session,
            release=release,
            cache=cache,
            destination=tmp_path / "projection.sqlite",
            dataset_names=("jquants.daily_bars",),
            builder_git_commit=_COMMIT,
            built_at=_BUILT_AT,
        )

        assert report.identity.source_release_id == "release-one"
        assert recording.reads.count(current_l1_pointer_key()) == 1

    def test_a_run_reads_only_lake_keys_and_never_the_legacy_sqlite(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        recording = RecordingSource(LocalMirrorSource(lake.mirror))
        _build(
            session,
            lake,
            destination=tmp_path / "projection.sqlite",
            cache=_cache(lake, recording=recording),
        )

        assert recording.reads
        assert all(key.startswith("lake/") for key in recording.reads)
        assert not any("market.sqlite" in key for key in recording.reads)

    def test_named_release_resolution_does_not_read_the_pointer(self, lake: Lake) -> None:
        recording = RecordingSource(LocalMirrorSource(lake.mirror))

        release = resolve_release(
            recording,
            lake.release_id,
            manifest_sha256=_release_digest(lake.mirror, lake.release_id),
        )

        assert release.release_id == lake.release_id
        assert current_l1_pointer_key() not in recording.reads

    def test_named_release_requires_the_pinned_manifest_digest(self, lake: Lake) -> None:
        with pytest.raises(LakeReadError, match="expected identity"):
            resolve_release(
                LocalMirrorSource(lake.mirror),
                lake.release_id,
                manifest_sha256="0" * 64,
            )

    def test_typed_release_ref_resolves_the_digest_bound_release(self, lake: Lake) -> None:
        digest = _release_digest(lake.mirror, lake.release_id)
        reference = L1ReleaseSourceRef(
            kind="l1_release",
            source_id=lake.release_id,
            key=release_manifest_key(release_id=lake.release_id),
            sha256=digest,
            manifest_version=1,
        )

        release = resolve_release_ref(LocalMirrorSource(lake.mirror), reference)

        assert release.release_id == lake.release_id
        assert release.manifest_sha256 == digest

    def test_previous_release_uses_the_digest_paired_on_current(self, lake: Lake) -> None:
        first_digest = _release_digest(lake.mirror, lake.release_id)
        _build_lake_second_release(lake)

        previous = resolve_previous_release(LocalMirrorSource(lake.mirror))

        assert previous.release_id == lake.release_id
        assert previous.manifest_sha256 == first_digest

        first_manifest = lake.mirror / release_manifest_key(release_id=lake.release_id)
        first_manifest.write_bytes(first_manifest.read_bytes() + b"\n")
        with pytest.raises(LakeReadError, match="expected identity"):
            resolve_previous_release(LocalMirrorSource(lake.mirror))

    def test_operational_current_rechecks_freshness_but_named_release_is_historical(
        self, lake: Lake
    ) -> None:
        source = LocalMirrorSource(lake.mirror)
        with pytest.raises(LakeReadError, match=r"operational policy.*freshness window"):
            _resolve_current_release_at(
                source,
                evaluated_at=_BUILT_AT + timedelta(days=367),
            )

        pinned = resolve_release(
            source,
            lake.release_id,
            manifest_sha256=_release_digest(lake.mirror, lake.release_id),
        )
        assert pinned.release_id == lake.release_id

        weekend = _resolve_current_release_at(
            source,
            evaluated_at=_BUILT_AT + timedelta(days=3),
        )
        assert weekend.release_id == lake.release_id

    def test_release_manifest_digest_mismatch_fails_closed(self, lake: Lake) -> None:
        pointer_path = lake.mirror / current_l1_pointer_key()
        payload = json.loads(pointer_path.read_text(encoding="utf-8"))
        payload["manifest_sha256"] = "0" * 64
        pointer_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(LakeReadError, match="digest does not match"):
            resolve_current_release(LocalMirrorSource(lake.mirror))

    def test_pointer_duplicate_field_is_rejected_by_the_reader(self, lake: Lake) -> None:
        pointer_path = lake.mirror / current_l1_pointer_key()
        payload = pointer_path.read_bytes().replace(
            b'"release_id":"release-one"',
            b'"release_id":"release-one","release_id":"release-other"',
        )
        pointer_path.write_bytes(payload)

        with pytest.raises(LakeReadError, match="current pointer is invalid"):
            resolve_current_release(LocalMirrorSource(lake.mirror))

    def test_pointer_validation_error_does_not_render_input_values(self, lake: Lake) -> None:
        pointer_path = lake.mirror / current_l1_pointer_key()
        pointer_path.write_bytes(
            pointer_path.read_bytes().replace(b"{", b'{"secret":"SENTINEL-DO-NOT-LOG",', 1)
        )

        with pytest.raises(LakeReadError, match="current pointer is invalid") as failure:
            resolve_current_release(LocalMirrorSource(lake.mirror))

        assert "SENTINEL-DO-NOT-LOG" not in str(failure.value)

    def test_release_duplicate_field_is_rejected_after_digest_verification(
        self, lake: Lake
    ) -> None:
        manifest_path = lake.mirror / release_manifest_key(release_id=lake.release_id)
        payload = manifest_path.read_bytes().replace(
            b'"release_id":"release-one"',
            b'"release_id":"release-one","release_id":"release-other"',
        )
        manifest_path.write_bytes(payload)
        pointer_path = lake.mirror / current_l1_pointer_key()
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer["manifest_sha256"] = hashlib.sha256(payload).hexdigest()
        pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

        with pytest.raises(LakeReadError, match="release manifest is invalid"):
            resolve_current_release(LocalMirrorSource(lake.mirror))

    def test_dataset_nested_duplicate_field_is_rejected_after_digest_verification(
        self, lake: Lake
    ) -> None:
        release_path = lake.mirror / release_manifest_key(release_id=lake.release_id)
        release_payload = json.loads(release_path.read_text(encoding="utf-8"))
        release = resolve_current_release(LocalMirrorSource(lake.mirror))
        manifest = release.dataset_manifest("jquants.daily_bars")
        manifest_path = (
            lake.mirror / f"lake/manifests/datasets/jquants.daily_bars/{manifest.build_id}.json"
        )
        manifest_payload = manifest_path.read_bytes().replace(b'"rows":', b'"rows":1,"rows":', 1)
        manifest_path.write_bytes(manifest_payload)
        release_payload["datasets"]["jquants.daily_bars"]["manifest_sha256"] = hashlib.sha256(
            manifest_payload
        ).hexdigest()
        updated_release = json.dumps(release_payload).encode()
        release_path.write_bytes(updated_release)
        pointer_path = lake.mirror / current_l1_pointer_key()
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer["manifest_sha256"] = hashlib.sha256(updated_release).hexdigest()
        pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

        with pytest.raises(LakeReadError, match="dataset manifest is invalid"):
            resolve_current_release(LocalMirrorSource(lake.mirror))

    def test_fixed_release_mappings_are_immutable_and_defensively_copied(self, lake: Lake) -> None:
        release = resolve_current_release(LocalMirrorSource(lake.mirror))
        original = dict(release.dataset_manifests)
        copied = replace(release, dataset_manifests=original)
        original.clear()

        assert copied.dataset_names() == release.dataset_names()
        with pytest.raises(TypeError):
            copied.dataset_manifests["mutated"] = release.dataset_manifest(  # type: ignore[index]
                "jquants.daily_bars"
            )
        with pytest.raises(TypeError):
            copied.dataset_manifest_sha256["mutated"] = "0" * 64  # type: ignore[index]

    def test_pointer_naming_another_release_manifest_key_fails_closed(self, lake: Lake) -> None:
        pointer_path = lake.mirror / current_l1_pointer_key()
        payload = json.loads(pointer_path.read_text(encoding="utf-8"))
        payload["manifest_key"] = "lake/manifests/releases/l1/release-other.json"
        pointer_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(LakeReadError, match="current pointer is invalid"):
            resolve_current_release(LocalMirrorSource(lake.mirror))

    def test_release_entry_that_disagrees_with_its_dataset_manifest_fails_closed(
        self, lake: Lake
    ) -> None:
        source = LocalMirrorSource(lake.mirror)
        payload = json.loads(
            (lake.mirror / release_manifest_key(release_id=lake.release_id)).read_text(
                encoding="utf-8"
            )
        )
        payload["release_id"] = "release-drifted"
        payload["datasets"]["jquants.daily_bars"]["contract_version"] = 2
        (lake.mirror / release_manifest_key(release_id="release-drifted")).write_text(
            json.dumps(payload), encoding="utf-8"
        )

        with pytest.raises(LakeReadError, match="disagree"):
            resolve_release(
                source,
                "release-drifted",
                manifest_sha256=_release_digest(lake.mirror, "release-drifted"),
            )

    def test_a_dataset_manifest_that_does_not_parse_fails_closed(self, lake: Lake) -> None:
        # Digest-consistent but structurally invalid: the object key no longer
        # matches the contract version the manifest declares.
        import hashlib

        release = resolve_current_release(LocalMirrorSource(lake.mirror))
        build_id = release.dataset_manifest("jquants.daily_bars").build_id
        key = f"lake/manifests/datasets/jquants.daily_bars/{build_id}.json"
        payload = json.loads((lake.mirror / key).read_text(encoding="utf-8"))
        payload["contract_version"] = 2
        broken = json.dumps(payload).encode()
        (lake.mirror / key).write_bytes(broken)
        release_payload = json.loads(
            (lake.mirror / release_manifest_key(release_id=lake.release_id)).read_text(
                encoding="utf-8"
            )
        )
        release_payload["release_id"] = "release-broken"
        release_payload["datasets"]["jquants.daily_bars"]["contract_version"] = 2
        release_payload["datasets"]["jquants.daily_bars"]["manifest_sha256"] = hashlib.sha256(
            broken
        ).hexdigest()
        (lake.mirror / release_manifest_key(release_id="release-broken")).write_text(
            json.dumps(release_payload), encoding="utf-8"
        )

        with pytest.raises(LakeReadError, match="dataset manifest is invalid"):
            resolve_release(
                LocalMirrorSource(lake.mirror),
                "release-broken",
                manifest_sha256=_release_digest(lake.mirror, "release-broken"),
            )

    def test_a_dataset_outside_the_contract_allowlist_is_not_readable(self, lake: Lake) -> None:
        release = resolve_current_release(LocalMirrorSource(lake.mirror))

        with pytest.raises(ValueError, match="unsupported lake dataset"):
            accepted_dataset(release, "jquants.daily_bars_v2")

    def test_a_dataset_manifest_replaced_under_the_same_key_fails_closed(
        self, lake: Lake, tmp_path: Path
    ) -> None:
        """The release must pin data, not just names.

        A dataset manifest key is derived from (dataset, build_id), so republishing
        that key would otherwise redirect an unchanged release to different objects,
        and every downstream digest check would pass — because those digests come
        from the replaced manifest.
        """

        source = LocalMirrorSource(lake.mirror)
        release = resolve_current_release(source)
        build_id = release.dataset_manifest("jquants.daily_bars").build_id
        key = f"lake/manifests/datasets/jquants.daily_bars/{build_id}.json"
        with sqlite3.connect(lake.sqlite_path) as connection:
            connection.execute("UPDATE jquants_daily_bars SET close = 999.0")
        replacement = export_legacy_sqlite(
            dataset_name="jquants.daily_bars",
            sqlite_path=lake.sqlite_path,
            mirror_root=tmp_path / "second-mirror",
            producer_git_commit=_COMMIT,
            build_id=build_id,
            created_at=_BUILT_AT,
        )
        (lake.mirror / key).write_bytes(replacement.manifest_path.read_bytes())

        with pytest.raises(LakeReadError, match="dataset manifest digest"):
            resolve_current_release(source)

    def test_a_dataset_publishing_another_contract_version_is_not_accepted(
        self, lake: Lake
    ) -> None:
        release = resolve_current_release(LocalMirrorSource(lake.mirror))
        manifest = release.dataset_manifest("jquants.daily_bars")
        next_contract = replace_manifest_contract(manifest, contract_version=2)
        future = replace(
            release,
            dataset_manifests={**release.dataset_manifests, "jquants.daily_bars": next_contract},
        )

        with pytest.raises(LakeReadError, match="this reader accepts only v1"):
            accepted_dataset(future, "jquants.daily_bars")

    def test_a_dataset_the_release_does_not_publish_is_not_readable(self, lake: Lake) -> None:
        release = resolve_current_release(LocalMirrorSource(lake.mirror))
        without_short_sale = replace(
            release,
            dataset_manifests={
                "jquants.daily_bars": release.dataset_manifests["jquants.daily_bars"]
            },
        )

        with pytest.raises(LakeReadError, match="does not contain dataset"):
            accepted_dataset(without_short_sale, "jquants.short_sale_reports")

    def test_requesting_a_month_the_release_does_not_publish_fails_closed(self, lake: Lake) -> None:
        release = resolve_current_release(LocalMirrorSource(lake.mirror))

        with pytest.raises(LakeReadError, match="2026-09"):
            selected_partitions(release, "jquants.daily_bars", months=[(2026, 9)])


class TestObjectIntegrity:
    def test_object_whose_bytes_differ_from_the_manifest_fails_closed(
        self, lake: Lake, tmp_path: Path
    ) -> None:
        release = resolve_current_release(LocalMirrorSource(lake.mirror))
        manifest = release.dataset_manifest("jquants.daily_bars")
        target = lake.mirror / manifest.partitions[0].objects[0].key
        target.write_bytes(target.read_bytes() + b"tamper")
        cache = LakeObjectCache(root=lake.mirror, source=LocalMirrorSource(lake.mirror))

        with pytest.raises(LakeObjectError, match="cached lake object differs"):
            cache.materialize(manifest.partitions[0].objects[0])

        assert target.is_file()

    def test_missing_object_fails_closed(self, lake: Lake) -> None:
        release = resolve_current_release(LocalMirrorSource(lake.mirror))
        manifest = release.dataset_manifest("jquants.daily_bars")
        lake_object = manifest.partitions[0].objects[0]
        (lake.mirror / lake_object.key).unlink()
        cache = LakeObjectCache(root=lake.mirror, source=LocalMirrorSource(lake.mirror))

        with pytest.raises(LakeObjectError, match="not readable"):
            cache.materialize(lake_object)

    def test_parquet_whose_schema_is_not_the_contract_fails_closed(
        self, lake: Lake, tmp_path: Path
    ) -> None:
        # The digest matches the bytes on purpose, so only the schema check can
        # reject this object. A publisher that changed the shape without changing
        # the contract would arrive exactly like this.
        foreign = tmp_path / "foreign.parquet"
        pq.write_table(pa.table({"ticker": ["1301"], "traded_at": ["2026-01-05"]}), foreign)
        lake_object = LakeObject(
            key="lake/l1/canonical/jquants.daily_bars/contract=v1/year=2026/month=1/"
            f"part-{sha256_file(foreign)}.parquet",
            sha256=sha256_file(foreign),
            bytes=foreign.stat().st_size,
            rows=1,
            min_key=("1301", "2026-01-05"),
            max_key=("1301", "2026-01-05"),
        )

        with pytest.raises(LakeReadError, match="schema does not match"):
            verify_object(foreign, JQUANTS_DAILY_BARS, lake_object)

    def test_parquet_whose_row_count_is_not_the_manifest_count_fails_closed(
        self, lake: Lake
    ) -> None:
        release = resolve_current_release(LocalMirrorSource(lake.mirror))
        manifest = release.dataset_manifest("jquants.daily_bars")
        lake_object = manifest.partitions[0].objects[0]
        path = lake.mirror / lake_object.key

        with pytest.raises(LakeReadError, match="row count differs"):
            verify_object(path, JQUANTS_DAILY_BARS, lake_object.model_copy(update={"rows": 99}))

    def test_path_traversal_in_an_object_key_is_rejected(self, lake: Lake) -> None:
        with pytest.raises(ValueError, match="unsafe path segment"):
            mirror_path(lake.mirror, "lake/l1/canonical/../../etc/passwd")

    def test_a_mirror_root_that_would_be_read_as_a_pattern_is_rejected(
        self, tmp_path: Path
    ) -> None:
        # Keys are pattern-free by allowlist, but read_parquet receives root + key
        # and expands each path as a glob, so the root has to be pattern-free too.
        for name in ("mirror*", "mirror?", "mirror[1]", "mirror{a}"):
            with pytest.raises(LakeObjectError, match="glob metacharacters"):
                mirror_root(tmp_path / name)

    def test_a_symlinked_key_that_escapes_the_root_is_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "loot.parquet").write_bytes(b"secret")
        root = tmp_path / "mirror"
        root.mkdir()
        (root / "lake").symlink_to(outside)

        with pytest.raises(LakeObjectError, match="escapes its root"):
            mirror_path(root, "lake/loot.parquet")

    def test_transfer_accounting_separates_fetched_bytes_from_reused_bytes(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        remote = tmp_path / "remote"
        remote.mkdir()
        for path in sorted(lake.mirror.rglob("*")):
            if path.is_file():
                target = remote / path.relative_to(lake.mirror)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        empty_cache_root = tmp_path / "cache"
        cache = LakeObjectCache(root=empty_cache_root, source=LocalMirrorSource(remote))
        release = resolve_release(
            LocalMirrorSource(remote),
            lake.release_id,
            manifest_sha256=_release_digest(remote, lake.release_id),
        )

        first = build_projection(
            session,
            release=release,
            cache=cache,
            destination=tmp_path / "projection.sqlite",
            dataset_names=("jquants.daily_bars",),
            builder_git_commit=_COMMIT,
            built_at=_BUILT_AT,
        )
        second_cache = LakeObjectCache(root=empty_cache_root, source=LocalMirrorSource(remote))
        build_projection(
            session,
            release=release,
            cache=second_cache,
            destination=tmp_path / "projection-2.sqlite",
            dataset_names=("jquants.daily_bars",),
            builder_git_commit=_COMMIT,
            built_at=_BUILT_AT,
        )

        assert first.transfers.fetched_objects == 2
        assert first.transfers.fetched_bytes > 0
        assert first.transfers.reused_objects == 0
        assert second_cache.transfers.fetched_objects == 0
        assert second_cache.transfers.reused_bytes == first.transfers.fetched_bytes


class TestProjection:
    def test_projection_holds_the_release_rows_and_its_source_identity(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        destination = tmp_path / "projection.sqlite"

        report = _build(session, lake, destination=destination)

        assert report.reused is False
        assert report.rows == {"jquants.daily_bars": 4, "jquants.short_sale_reports": 2}
        with sqlite3.connect(destination) as connection:
            bars = connection.execute(
                "SELECT ticker, traded_at, close FROM jquants_daily_bars ORDER BY ticker, traded_at"
            ).fetchall()
            indexes = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%' "
                "ORDER BY name"
            ).fetchall()
        assert bars == [
            ("1301", "2026-01-05", 100.0),
            ("1301", "2026-01-20", 105.0),
            ("1301", "2026-02-02", 110.0),
            ("7203", "2026-01-05", 200.0),
        ]
        assert [name for (name,) in indexes] == [
            "idx_jquants_daily_bars_traded_at",
            "idx_jquants_short_sale_reports_ticker",
        ]
        identity = read_projection_identity(destination)
        assert identity is not None
        assert (
            identity.source_release_manifest_sha256
            == report.identity.source_release_manifest_sha256
        )

    def test_projection_shape_matches_the_legacy_sqlite_contract(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        """Columns and indexes both, since a drop-in replacement is judged on both.

        The projection declares its own DDL, so a change to the market schema that
        is not mirrored here has to turn this red. Comparing structure rather than
        DDL text keeps the check independent of how each side spells its statement.
        """

        destination = tmp_path / "projection.sqlite"
        _build(session, lake, destination=destination)

        for dataset in (JQUANTS_DAILY_BARS, JQUANTS_SHORT_SALE_REPORTS):
            with (
                sqlite3.connect(destination) as projection,
                sqlite3.connect(lake.sqlite_path) as legacy,
            ):
                assert _table_shape(projection, dataset.sqlite_table) == _table_shape(
                    legacy, dataset.sqlite_table
                )

    def test_identical_inputs_reuse_the_projection_without_rebuilding(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        first = _build(session, lake, destination=destination)
        stamp = destination.stat().st_mtime_ns

        second = _build(session, lake, destination=destination)

        assert second.reused is True
        assert second.identity == first.identity
        assert destination.stat().st_mtime_ns == stamp

    def test_a_commit_that_changes_no_projection_input_reuses(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        first = _build(session, lake, destination=destination)

        second = _build(session, lake, destination=destination, commit=_OTHER_COMMIT)

        assert second.reused is True
        assert second.identity == first.identity

    def test_a_changed_projection_implementation_rebuilds(
        self, session: LakeSession, lake: Lake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        first = _build(session, lake, destination=destination)

        monkeypatch.setattr(projection_module, "PROJECTION_CONTRACT_VERSION", 99)
        second = _build(session, lake, destination=destination)

        assert second.reused is False
        assert second.identity.projection_fingerprint != first.identity.projection_fingerprint

    def test_a_different_release_rebuilds(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        _build(session, lake, destination=destination)
        with sqlite3.connect(lake.sqlite_path) as connection:
            connection.execute(
                "UPDATE jquants_daily_bars SET close = 999.0 WHERE traded_at = '2026-02-02'"
            )
        rebuilt = _build_lake_second_release(lake)

        cache = _cache(lake)
        release = resolve_current_release(cache.source)
        report = build_projection(
            session,
            release=release,
            cache=cache,
            destination=destination,
            dataset_names=("jquants.daily_bars", "jquants.short_sale_reports"),
            builder_git_commit=_COMMIT,
            built_at=_BUILT_AT,
        )

        assert report.reused is False
        assert report.identity.source_release_id == rebuilt
        with sqlite3.connect(destination) as connection:
            close = connection.execute(
                "SELECT close FROM jquants_daily_bars WHERE traded_at = '2026-02-02'"
            ).fetchone()
        assert close == (999.0,)

    def test_deleting_the_projection_rebuilds_it_from_the_release(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        first = _build(session, lake, destination=destination)
        destination.unlink()

        second = _build(session, lake, destination=destination)

        assert second.reused is False
        assert second.identity == first.identity

    def test_metadata_is_deterministic_for_the_same_release_and_commit(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        first = _build(session, lake, destination=tmp_path / "one.sqlite")
        second = _build(session, lake, destination=tmp_path / "two.sqlite")

        assert first.identity == second.identity
        assert first.identity.projection_fingerprint == projection_fingerprint(
            [JQUANTS_DAILY_BARS, JQUANTS_SHORT_SALE_REPORTS]
        )

    def test_a_partial_projection_is_not_published_and_leaves_the_previous_one(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        first = _build(session, lake, destination=destination)
        before = destination.read_bytes()
        release = resolve_current_release(LocalMirrorSource(lake.mirror))
        manifest = release.dataset_manifest("jquants.short_sale_reports")
        (lake.mirror / manifest.partitions[0].objects[0].key).unlink()

        with pytest.raises(LakeObjectError):
            build_projection(
                session,
                release=release,
                cache=_cache(lake),
                destination=destination,
                dataset_names=("jquants.daily_bars", "jquants.short_sale_reports"),
                builder_git_commit=_OTHER_COMMIT,
                force=True,
                built_at=_BUILT_AT,
            )

        assert destination.read_bytes() == before
        assert read_projection_identity(destination) == first.identity
        assert not [path for path in tmp_path.iterdir() if path.name.endswith(".building")]

    def test_a_truncated_projection_reports_no_identity(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        _build(session, lake, destination=destination)
        destination.write_bytes(b"not a database")

        assert read_projection_identity(destination) is None

    def test_force_rebuilds_even_when_the_identity_matches(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        _build(session, lake, destination=destination)

        assert _build(session, lake, destination=destination, force=True).reused is False

    def test_rows_removed_without_touching_the_metadata_still_rebuild(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        first = _build(session, lake, destination=destination)
        with sqlite3.connect(destination) as connection:
            connection.execute("DELETE FROM jquants_daily_bars WHERE traded_at = '2026-02-02'")

        second = _build(session, lake, destination=destination)

        assert second.reused is False
        assert second.rows == first.rows
        with sqlite3.connect(destination) as connection:
            restored = connection.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()
        assert restored == (4,)

    def test_value_mutation_with_the_same_row_count_rebuilds(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        _build(session, lake, destination=destination)
        with sqlite3.connect(destination) as connection:
            connection.execute(
                "UPDATE jquants_daily_bars SET close = 999.0 "
                "WHERE ticker = '1301' AND traded_at = '2026-01-20'"
            )

        report = _build(session, lake, destination=destination)

        assert report.reused is False
        with sqlite3.connect(destination) as connection:
            restored = connection.execute(
                "SELECT close FROM jquants_daily_bars "
                "WHERE ticker = '1301' AND traded_at = '2026-01-20'"
            ).fetchone()
        assert restored == (105.0,)

    def test_schema_mutation_rebuilds(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        _build(session, lake, destination=destination)
        with sqlite3.connect(destination) as connection:
            connection.execute("ALTER TABLE jquants_daily_bars ADD COLUMN injected TEXT")

        report = _build(session, lake, destination=destination)

        assert report.reused is False
        with sqlite3.connect(destination) as connection:
            names = [
                str(row[1]) for row in connection.execute("PRAGMA table_info(jquants_daily_bars)")
            ]
        assert "injected" not in names

    def test_index_mutation_rebuilds(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        _build(session, lake, destination=destination)
        with sqlite3.connect(destination) as connection:
            connection.execute("DROP INDEX idx_jquants_daily_bars_traded_at")

        report = _build(session, lake, destination=destination)

        assert report.reused is False
        with sqlite3.connect(destination) as connection:
            index = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND name = 'idx_jquants_daily_bars_traded_at'"
            ).fetchone()
        assert index == ("idx_jquants_daily_bars_traded_at",)

    def test_partial_index_with_the_expected_name_and_columns_rebuilds(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        _build(session, lake, destination=destination)
        with sqlite3.connect(destination) as connection:
            connection.execute("DROP INDEX idx_jquants_daily_bars_traded_at")
            connection.execute(
                "CREATE INDEX idx_jquants_daily_bars_traded_at "
                "ON jquants_daily_bars(traded_at) WHERE ticker = '1301'"
            )

        report = _build(session, lake, destination=destination)

        assert report.reused is False
        with sqlite3.connect(destination) as connection:
            indexes = connection.execute("PRAGMA index_list(jquants_daily_bars)").fetchall()
        assert next(row for row in indexes if row[1] == "idx_jquants_daily_bars_traded_at")[4] == 0

    def test_case_insensitive_filesystem_is_rejected_before_destination_change(
        self,
        session: LakeSession,
        lake: Lake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        _build(session, lake, destination=destination)
        before = destination.read_bytes()
        monkeypatch.setattr(Path, "samefile", lambda _self, _other: True)

        with pytest.raises(ProjectionError, match="case-sensitive"):
            _build(session, lake, destination=destination, force=True)

        assert destination.read_bytes() == before
        assert not [path for path in tmp_path.iterdir() if path.name.endswith(".probe")]

    def test_insufficient_capacity_fails_before_changing_the_destination(
        self,
        session: LakeSession,
        lake: Lake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        _build(session, lake, destination=destination)
        before = destination.read_bytes()
        monkeypatch.setattr(
            shutil,
            "disk_usage",
            lambda _path: SimpleNamespace(free=0),
        )

        with pytest.raises(ProjectionError, match="free bytes"):
            _build(session, lake, destination=destination, force=True)

        assert destination.read_bytes() == before
        assert not [path for path in tmp_path.iterdir() if path.name.endswith(".building")]
        assert not [path for path in tmp_path.iterdir() if path.name.endswith(".rollback")]

    def test_failed_atomic_replace_leaves_the_previous_projection(
        self,
        session: LakeSession,
        lake: Lake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        _build(session, lake, destination=destination)
        before = destination.read_bytes()
        real_replace = Path.replace

        def fail_replace(source: Path, target: Path) -> Path:
            if source.name.endswith(".building"):
                raise OSError("simulated replace failure")
            return real_replace(source, target)

        monkeypatch.setattr(Path, "replace", fail_replace)

        with pytest.raises(OSError, match="replace failure"):
            _build(session, lake, destination=destination, force=True)

        assert destination.read_bytes() == before
        assert not [path for path in tmp_path.iterdir() if path.name.endswith(".building")]
        assert not [path for path in tmp_path.iterdir() if path.name.endswith(".rollback")]

    def test_post_replace_directory_fsync_failure_restores_the_previous_projection(
        self,
        session: LakeSession,
        lake: Lake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        destination = tmp_path / "projection.sqlite"
        _build(session, lake, destination=destination)
        before = destination.read_bytes()
        real_fsync = os.fsync
        real_replace = Path.replace
        candidate_visible = False
        injected = False

        def record_candidate_replace(source: Path, target: Path) -> Path:
            nonlocal candidate_visible
            result = real_replace(source, target)
            if source.name.endswith(".building"):
                candidate_visible = True
            return result

        def fail_post_replace_fsync(file_descriptor: int) -> None:
            nonlocal injected
            if candidate_visible and not injected:
                injected = True
                raise OSError("simulated post-replace fsync failure")
            real_fsync(file_descriptor)

        monkeypatch.setattr(Path, "replace", record_candidate_replace)
        monkeypatch.setattr(os, "fsync", fail_post_replace_fsync)

        with pytest.raises(ProjectionError, match="previous generation was restored"):
            _build(session, lake, destination=destination, force=True)

        assert destination.read_bytes() == before
        assert not [path for path in tmp_path.iterdir() if path.name.endswith(".rollback")]

    def test_a_database_that_is_not_a_projection_is_never_replaced(
        self, session: LakeSession, lake: Lake
    ) -> None:
        # A mistyped destination must not destroy the store it points at.
        before = lake.sqlite_path.read_bytes()

        with pytest.raises(ProjectionError, match="not a projection"):
            _build(session, lake, destination=lake.sqlite_path)

        assert lake.sqlite_path.read_bytes() == before

    def test_a_file_that_is_not_a_database_is_never_replaced(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        # Refusing only readable databases would leave YAML, Parquet, and dotfiles
        # exposed to the same mistyped path.
        for name, payload in (
            ("notes.yaml", b"asof: 2026-08-12\n"),
            ("object.parquet", b"PAR1nonsense"),
            (".env", b"R2_ACCESS_KEY_ID=x\n"),
        ):
            destination = tmp_path / name
            destination.write_bytes(payload)

            with pytest.raises(ProjectionError, match="not a projection"):
                _build(session, lake, destination=destination)

            assert destination.read_bytes() == payload


class TestExplicitObjectReads:
    def test_reads_name_the_manifest_objects_and_reject_an_unknown_column(
        self, session: LakeSession, lake: Lake
    ) -> None:
        release = resolve_current_release(LocalMirrorSource(lake.mirror))
        dataset = accepted_dataset(release, "jquants.daily_bars")
        cache = _cache(lake)
        paths = [
            cache.materialize(item)
            for partition in selected_partitions(release, dataset.name)
            for item in partition.objects
        ]

        rows = [
            row
            for batch in iter_partition_rows(
                session, dataset=dataset, paths=paths, columns=("ticker", "close")
            )
            for row in batch
        ]

        assert rows == [
            ("1301", 100.0),
            ("1301", 105.0),
            ("1301", 110.0),
            ("7203", 200.0),
        ]
        with pytest.raises(LakeReadError, match="has no column"):
            next(iter_partition_rows(session, dataset=dataset, paths=paths, columns=("secret",)))

    def test_rows_arrive_in_bounded_batches_rather_than_one_list(
        self, session: LakeSession, lake: Lake
    ) -> None:
        """Peak memory has to follow the batch size, not the dataset size.

        A decade of daily bars is eight figures of rows; converting them all into
        Python objects at once is what would make a real projection build unbuildable.
        """

        release = resolve_current_release(LocalMirrorSource(lake.mirror))
        dataset = accepted_dataset(release, "jquants.daily_bars")
        cache = _cache(lake)
        paths = [
            cache.materialize(item)
            for partition in selected_partitions(release, dataset.name)
            for item in partition.objects
        ]

        batches = list(iter_partition_rows(session, dataset=dataset, paths=paths, batch_size=1))

        assert [len(batch) for batch in batches] == [1, 1, 1, 1]

    def test_a_non_positive_batch_size_is_refused(self, session: LakeSession, lake: Lake) -> None:
        release = resolve_current_release(LocalMirrorSource(lake.mirror))
        dataset = accepted_dataset(release, "jquants.daily_bars")
        cache = _cache(lake)
        paths = [
            cache.materialize(item)
            for partition in selected_partitions(release, dataset.name)
            for item in partition.objects
        ]

        with pytest.raises(LakeReadError, match="batch size must be positive"):
            next(iter_partition_rows(session, dataset=dataset, paths=paths, batch_size=0))


class TestCredentialBoundary:
    def test_remote_authorization_loads_but_never_installs_httpfs(self) -> None:
        class RecordingConnection:
            def __init__(self) -> None:
                self.statements: list[str] = []

            def execute(self, statement: str, _parameters: object = None) -> None:
                self.statements.append(statement)

        connection = RecordingConnection()
        credentials = R2ReadCredentials(
            account_id="a" * 32,
            access_key_id="K" * 32,
            secret_access_key="super-secret-value",
            bucket="baibai-stores",
        )
        session = LakeSession(connection=connection, credentials=credentials)  # type: ignore[arg-type]

        duck_module._authorize_r2(session, credentials)

        assert connection.statements[0] == "LOAD httpfs"
        assert not any(statement.startswith("INSTALL") for statement in connection.statements)

    def test_credentials_are_not_rendered(self) -> None:
        credentials = R2ReadCredentials(
            account_id="a" * 32,
            access_key_id="K" * 32,
            secret_access_key="super-secret-value",
            bucket="baibai-stores",
        )

        assert "super-secret-value" not in repr(credentials)
        assert "super-secret-value" not in str(credentials)
        assert "a" * 32 not in repr(credentials)

    def test_redaction_removes_every_credential_component_from_a_message(self) -> None:
        credentials = R2ReadCredentials(
            account_id="a" * 32,
            access_key_id="K" * 32,
            secret_access_key="super-secret-value",
            bucket="baibai-stores",
        )
        session = LakeSession(connection=None, credentials=credentials)  # type: ignore[arg-type]
        message = (
            f"HTTP 403 for https://{'a' * 32}.r2.cloudflarestorage.com/x "
            f"key={'K' * 32} secret=super-secret-value"
        )

        redacted = session.redact(message)

        assert "super-secret-value" not in redacted
        assert "K" * 32 not in redacted
        assert "a" * 32 not in redacted

    def test_a_malformed_account_id_is_refused_before_any_connection(self) -> None:
        with pytest.raises(LakeCredentialError, match="R2_ACCOUNT_ID"):
            R2ReadCredentials(
                account_id="not-hex",
                access_key_id="K" * 32,
                secret_access_key="s",
                bucket="baibai-stores",
            )

    def test_missing_environment_variables_are_named(self) -> None:
        with pytest.raises(LakeCredentialError, match="R2_ACCESS_KEY_ID"):
            R2ReadCredentials.from_env({"R2_ACCOUNT_ID": "a" * 32})

    def test_a_session_without_credentials_cannot_produce_a_remote_uri(self) -> None:
        from baibai_engine.market.lake.objects import R2ObjectSource

        with (
            lake_session() as session,
            pytest.raises(LakeObjectError, match="authorised session"),
        ):
            R2ObjectSource(session).uri("lake/pointers/l1/current.json")

    def test_a_session_without_credentials_cannot_reach_a_remote_path(self) -> None:
        """Extension autoload is off, so a remote path fails instead of pulling httpfs in."""

        import duckdb

        with lake_session() as session:
            assert session.connection.execute(
                "SELECT current_setting('autoload_known_extensions'), "
                "current_setting('autoinstall_known_extensions')"
            ).fetchone() == (False, False)
            with pytest.raises(duckdb.Error, match="httpfs"):
                session.connection.execute(
                    "SELECT * FROM read_blob('s3://bucket/lake/pointers/l1/current.json')"
                ).fetchall()


def _build_lake_second_release(lake: Lake) -> str:
    """Publish a second release over the same mirror and switch the pointer to it."""

    release_id = "release-two"
    snapshot = capture_legacy_sqlite_snapshot(
        sqlite_path=lake.sqlite_path,
        mirror_root=lake.mirror,
        snapshot_id=f"snapshot-{release_id}",
    )
    manifests = [
        export_legacy_sqlite(
            dataset_name=name,
            sqlite_path=lake.sqlite_path,
            mirror_root=lake.mirror,
            producer_git_commit=_COMMIT,
            source_snapshot_ref=snapshot.ref,
            build_id=f"build-{name.replace('.', '-')}-{release_id}",
            created_at=_BUILT_AT,
        ).manifest_path
        for name in ("jquants.daily_bars", "jquants.short_sale_reports")
    ]
    manifest_path, _release = create_l1_release(
        dataset_manifest_paths=manifests,
        mirror_root=lake.mirror,
        release_id=release_id,
        created_at=_BUILT_AT,
    )
    _publish_pointer(lake.mirror, release_id, manifest_path)
    return release_id


class TestProjectionConcurrency:
    def test_a_second_builder_cannot_publish_the_same_destination_concurrently(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        """Serialising the destination is what keeps an older release from landing last."""

        destination = tmp_path / "projection.sqlite"
        with (
            exclusive_lock(
                destination.with_name(f".{destination.name}.lock"), subject="projection destination"
            ),
            pytest.raises(LakeRetentionError, match="projection destination"),
        ):
            _build(session, lake, destination=destination)

        assert not destination.exists()
        assert _build(session, lake, destination=destination).reused is False


class TestCacheResilience:
    def test_a_damaged_cached_object_is_refetched_from_the_remote_source(
        self, lake: Lake, tmp_path: Path
    ) -> None:
        remote = tmp_path / "remote"
        for path in sorted(lake.mirror.rglob("*")):
            if path.is_file():
                target = remote / path.relative_to(lake.mirror)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        release = resolve_current_release(LocalMirrorSource(remote))
        lake_object = release.dataset_manifest("jquants.daily_bars").partitions[0].objects[0]
        cache_root = tmp_path / "cache"
        cache = LakeObjectCache(root=cache_root, source=LocalMirrorSource(remote))
        cache.materialize(lake_object)
        cached = cache_root / lake_object.key
        cached.write_bytes(b"rot")

        path = cache.materialize(lake_object)

        assert path.read_bytes() == (remote / lake_object.key).read_bytes()
        assert cache.transfers.fetched_objects == 2

    def test_a_cache_that_is_its_own_source_fails_closed_instead_of_looping(
        self, lake: Lake
    ) -> None:
        release = resolve_current_release(LocalMirrorSource(lake.mirror))
        lake_object = release.dataset_manifest("jquants.daily_bars").partitions[0].objects[0]
        (lake.mirror / lake_object.key).write_bytes(b"rot")
        cache = LakeObjectCache(root=lake.mirror, source=LocalMirrorSource(lake.mirror))

        with pytest.raises(LakeObjectError, match="cached lake object differs"):
            cache.materialize(lake_object)

        assert (lake.mirror / lake_object.key).read_bytes() == b"rot"
