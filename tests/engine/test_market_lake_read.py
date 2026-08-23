from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from baibai_engine.market.lake import duck as duck_module
from baibai_engine.market.lake import hydrate as hydrate_module
from baibai_engine.market.lake import models as lake_models
from baibai_engine.market.lake import prefetch as prefetch_module
from baibai_engine.market.lake.datasets import JQUANTS_DAILY_BARS
from baibai_engine.market.lake.duck import (
    LakeCredentialError,
    LakeSession,
    R2ReadCredentials,
    lake_session,
)
from baibai_engine.market.lake.hydrate import (
    LakeHydrateError,
    dehydrate_market_store,
    hydrate_market_store,
)
from baibai_engine.market.lake.keys import (
    canonical_object_key,
    current_l1_pointer_key,
    release_manifest_key,
)
from baibai_engine.market.lake.models import (
    DatasetManifest,
    L1ReleaseSourceRef,
    LakeObject,
    canonical_lake_model_bytes,
)
from baibai_engine.market.lake.objects import (
    LakeObjectCache,
    LakeObjectError,
    LocalMirrorSource,
    mirror_path,
    mirror_root,
    sha256_file,
)
from baibai_engine.market.lake.prefetch import (
    PrefetchingSource,
    hydration_order,
    prefetching_hydration_cache,
)
from baibai_engine.market.lake.reader import (
    LakeReadError,
    accepted_dataset,
    iter_partition_rows,
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
    create_l1_release,
)
from baibai_engine.market.lake.retention import LakeRetentionError, exclusive_lock
from baibai_engine.market.lake.writer import (
    export_lake_legacy,
    export_legacy_sqlite,
    sealed_sqlite_snapshot,
)
from baibai_engine.market.sqlite import SQLITE_SCHEMA_VERSION, open_connection
from baibai_engine.market.sqlite.lake_origin import (
    LakeStoreOrigin,
    advance_lake_store_origin,
    read_lake_store_origin,
    write_lake_store_origin,
)

_COMMIT = "b" * 40
_OTHER_COMMIT = "c" * 40
_BUILT_AT = datetime(2026, 8, 12, 3, 0, tzinfo=UTC)


def resolve_current_release(source: object):
    return _resolve_current_release_at(source, evaluated_at=_BUILT_AT)  # type: ignore[arg-type]


# Captured before any fixture narrows the module attribute, so a fixture that narrows it
# further starts from the real profile rather than from another fixture's leftovers.
_FULL_RELEASE_POLICY = lake_models.PRODUCTION_RELEASE_POLICY


@pytest.fixture(autouse=True)
def _small_pilot_release_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    # These fixtures build the two datasets these tests exercise, so the
    # profile is narrowed to them: a policy that still required the other thirteen would
    # refuse every fixture release for being incomplete, which is a fact about the
    # fixture rather than about the code under test.
    datasets = tuple(
        item.model_copy(
            update={
                "coverage_start_on_or_before": date.max,
                "minimum_rows": 1,
                "minimum_population_count": 1,
                "max_age_days": 366,
                "max_lead_days": 366,
            }
        )
        for item in _FULL_RELEASE_POLICY.datasets
        if item.dataset in {"jquants.daily_bars", "jquants.short_sale_reports"}
    )
    monkeypatch.setattr(
        lake_models,
        "PRODUCTION_RELEASE_POLICY",
        _FULL_RELEASE_POLICY.model_copy(update={"datasets": datasets}),
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
    pointer = L1ReleasePointer(
        release_id=release_id,
        manifest_key=manifest_path.relative_to(mirror).as_posix(),
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(canonical_lake_model_bytes(pointer))


def _release_digest(mirror: Path, release_id: str) -> str:
    path = mirror / release_manifest_key(release_id=release_id)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_lake(root: Path, *, release_id: str = "release-one") -> Lake:
    sqlite_path = _market_store(root / "market.sqlite")
    mirror = root / "mirror"
    with sealed_sqlite_snapshot(
        sqlite_path=sqlite_path, mirror_root=mirror, snapshot_id=f"snapshot-{release_id}"
    ) as snapshot:
        manifests = [
            export_legacy_sqlite(
                dataset_name=name,
                mirror_root=mirror,
                producer_git_commit=_COMMIT,
                source_snapshot=snapshot,
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
    connection = open_connection(sqlite_path)
    write_lake_store_origin(
        connection,
        LakeStoreOrigin(
            release_id=release_id,
            release_manifest_sha256=_release_digest(mirror, release_id),
        ),
    )
    connection.commit()
    connection.close()
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

        report = hydrate_market_store(
            session,
            release=release,
            cache=cache,
            store=_dehydrated(lake, tmp_path / "arrived.sqlite"),
            dataset_names=("jquants.daily_bars",),
        )

        assert report.release_id == "release-one"
        assert recording.reads.count(current_l1_pointer_key()) == 1

    def test_a_run_reads_only_lake_keys_and_never_the_legacy_sqlite(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        recording = RecordingSource(LocalMirrorSource(lake.mirror))
        cache = _cache(lake, recording=recording)
        hydrate_market_store(
            session,
            release=resolve_current_release(cache.source),
            cache=cache,
            store=_dehydrated(lake, tmp_path / "arrived.sqlite"),
            dataset_names=("jquants.daily_bars", "jquants.short_sale_reports"),
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

    @pytest.mark.parametrize(
        "restate",
        [
            lambda entry: entry.update(coverage_status="partial"),
            lambda entry: entry["totals"].update(rows=entry["totals"]["rows"] + 1),
            lambda entry: entry["totals"].update(objects=entry["totals"]["objects"] + 1),
        ],
        ids=["coverage_status", "rows", "objects"],
    )
    def test_a_named_release_that_restates_its_manifest_wrongly_is_refused(
        self, lake: Lake, restate: Callable[[dict[str, Any]], None]
    ) -> None:
        """Exempting freshness on a historical read is not exempting inventory.

        A pinned study reads a release the pointer left behind, so the age check has to
        go. What the release *says* about the datasets it names — the watermark, the
        coverage, the totals — is time-independent, and it is what a reader reports about
        the generation. An entry that disagrees with the manifest it addresses describes
        data that is not there.
        """

        path = lake.mirror / release_manifest_key(release_id=lake.release_id)
        payload = json.loads(path.read_bytes())
        entry = payload["datasets"]["jquants.daily_bars"]
        restate(entry)
        tampered = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        path.write_bytes(tampered)

        with pytest.raises(LakeReadError, match="disagree"):
            resolve_release(
                LocalMirrorSource(lake.mirror),
                lake.release_id,
                manifest_sha256=hashlib.sha256(tampered).hexdigest(),
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
        with sealed_sqlite_snapshot(
            sqlite_path=lake.sqlite_path, mirror_root=tmp_path / "second-mirror"
        ) as snapshot:
            replacement = export_legacy_sqlite(
                dataset_name="jquants.daily_bars",
                mirror_root=tmp_path / "second-mirror",
                producer_git_commit=_COMMIT,
                source_snapshot=snapshot,
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
            selected_partitions(release, "jquants.daily_bars", periods=[(2026, 9)])

    def test_a_yearly_dataset_is_selected_through_its_own_layout(self, lake: Lake) -> None:
        """A reader that assumed year/month read every yearly dataset as a KeyError.

        The fixtures are month-grained, so the yearly path reached the reader for the
        first time against the real store rather than here. Restating one manifest under
        the layout a yearly dataset publishes puts that path back under test without
        making every fixture carry a second dataset.
        """

        release = resolve_current_release(LocalMirrorSource(lake.mirror))
        monthly = release.dataset_manifest("jquants.daily_bars")
        yearly = monthly.model_copy(
            update={
                "partition_by": ("year",),
                "partitions": tuple(
                    partition.model_copy(update={"values": {"year": partition.values["year"]}})
                    for partition in monthly.partitions[:1]
                ),
            }
        )
        fixed = replace(release, dataset_manifests={"jquants.daily_bars": yearly})

        selected = selected_partitions(fixed, "jquants.daily_bars", periods=[(2026,)])
        assert [item.values for item in selected] == [{"year": 2026}]

        with pytest.raises(LakeReadError, match="2025"):
            selected_partitions(fixed, "jquants.daily_bars", periods=[(2025,)])


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

        first = hydrate_market_store(
            session,
            release=release,
            cache=cache,
            store=_dehydrated(lake, tmp_path / "first.sqlite"),
            dataset_names=("jquants.daily_bars",),
        )
        second_cache = LakeObjectCache(root=empty_cache_root, source=LocalMirrorSource(remote))
        hydrate_market_store(
            session,
            release=release,
            cache=second_cache,
            store=_dehydrated(lake, tmp_path / "second.sqlite"),
            dataset_names=("jquants.daily_bars",),
        )

        assert first.transfers.fetched_objects == 2
        assert first.transfers.fetched_bytes > 0
        assert first.transfers.reused_objects == 0
        assert second_cache.transfers.fetched_objects == 0
        assert second_cache.transfers.reused_bytes == first.transfers.fetched_bytes


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
        Python objects at once is what would make a real fill unbuildable.
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
    with sealed_sqlite_snapshot(
        sqlite_path=lake.sqlite_path,
        mirror_root=lake.mirror,
        snapshot_id=f"snapshot-{release_id}",
    ) as snapshot:
        manifests = [
            export_legacy_sqlite(
                dataset_name=name,
                mirror_root=lake.mirror,
                producer_git_commit=_COMMIT,
                source_snapshot=snapshot,
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


class TestReleaseCurrency:
    def test_a_second_fill_cannot_publish_the_same_store_concurrently(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        """Serialising the store is what keeps an older release from landing last."""

        store = _dehydrated(lake, tmp_path / "arrived.sqlite")
        with (
            exclusive_lock(store.with_name(f".{store.name}.lock"), subject="market store"),
            pytest.raises(LakeRetentionError, match="market store"),
        ):
            _hydrate(session, lake, store)

        connection = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        try:
            assert connection.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0] == 0
        finally:
            connection.close()
        assert _hydrate(session, lake, store).rows["jquants.daily_bars"] == 4

    def test_a_fill_that_resolved_an_older_release_does_not_replace_a_newer_one(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        """Serialising is not enough: the loser of the race can hold the older release.

        A fill resolves the pointer before it queues for the store, so the release it
        holds may be superseded while it waits. Installing it anyway would move the
        store back to a generation the pointer has already left, and the store would be
        internally valid the whole time — nothing downstream could tell it apart from
        the newer one except by the release the fill recorded.
        """

        store = _dehydrated(lake, tmp_path / "arrived.sqlite")
        cache = _cache(lake)
        stale = resolve_current_release(cache.source)
        newer = _build_lake_second_release(lake)
        assert newer != stale.release_id

        with pytest.raises(LakeHydrateError, match="no longer current"):
            hydrate_market_store(
                session,
                release=stale,
                cache=cache,
                store=store,
                dataset_names=("jquants.daily_bars", "jquants.short_sale_reports"),
                still_current=lambda: (
                    resolve_current_release(cache.source).release_id,
                    resolve_current_release(cache.source).manifest_sha256,
                ),
            )

        connection = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        try:
            assert connection.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0] == 0
        finally:
            connection.close()

    def test_a_release_id_reused_for_other_bytes_does_not_pass_the_currency_check(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        """A name is not an identity: recovery tools are exactly what reuse an ID.

        Comparing only the release id lets a manifest republished under an existing name
        satisfy the check while pointing at a different graph, and the store filled from
        the old bytes takes the destination.
        """

        store = _dehydrated(lake, tmp_path / "arrived.sqlite")
        cache = _cache(lake)
        release = resolve_current_release(cache.source)

        with pytest.raises(LakeHydrateError, match="no longer current"):
            hydrate_market_store(
                session,
                release=release,
                cache=cache,
                store=store,
                dataset_names=("jquants.daily_bars", "jquants.short_sale_reports"),
                still_current=lambda: (release.release_id, "0" * 64),
            )

    def test_an_explicitly_named_release_is_filled_without_a_currency_check(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        """Asking for one named generation is a statement, not a race to be arbitrated."""

        store = _dehydrated(lake, tmp_path / "historical.sqlite")
        cache = _cache(lake)
        named = resolve_current_release(cache.source)
        _build_lake_second_release(lake)

        report = hydrate_market_store(
            session,
            release=named,
            cache=cache,
            store=store,
            dataset_names=("jquants.daily_bars", "jquants.short_sale_reports"),
        )

        assert report.release_id == named.release_id


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


def _dehydrated(lake: Lake, path: Path) -> Path:
    """A copy of the fixture store shaped like the one R2 holds after the cutover."""

    shutil.copyfile(lake.sqlite_path, path)
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM jquants_daily_bars")
    connection.execute("DELETE FROM jquants_short_sale_reports")
    connection.commit()
    connection.close()
    return path


def _hydrate(session: LakeSession, lake: Lake, store: Path, **kwargs: Any):
    cache = _cache(lake)
    return hydrate_market_store(
        session,
        release=resolve_current_release(cache.source),
        cache=cache,
        store=store,
        dataset_names=("jquants.daily_bars", "jquants.short_sale_reports"),
        **kwargs,
    )


class TestHydrate:
    def test_filling_restores_the_published_rows_and_leaves_the_rest_of_the_store_alone(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        store = _dehydrated(lake, tmp_path / "arrived.sqlite")
        before = sqlite3.connect(f"file:{lake.sqlite_path}?mode=ro", uri=True)
        expected_shape = {
            table: _table_shape(before, table)
            for table in ("jquants_daily_bars", "jquants_short_sale_reports")
        }
        expected_version = before.execute("PRAGMA user_version").fetchone()[0]
        before.close()

        report = _hydrate(session, lake, store)

        assert report.rows == {"jquants.daily_bars": 4, "jquants.short_sale_reports": 2}
        assert report.release_id == lake.release_id
        origin = read_lake_store_origin(store)
        assert origin is not None
        assert origin.release_id == lake.release_id
        assert origin.release_manifest_sha256 == report.release_manifest_sha256
        filled = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        try:
            assert filled.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0] == 4
            # The coverage ledger is what the lake does not own, and the fill has to be
            # the reason it survives rather than an accident of the copy.
            assert filled.execute("SELECT COUNT(*) FROM source_coverage").fetchone()[0] == 1
            assert filled.execute("PRAGMA user_version").fetchone()[0] == expected_version
            for table, shape in expected_shape.items():
                assert _table_shape(filled, table) == shape
        finally:
            filled.close()

    def test_filling_refuses_over_rows_the_release_cannot_give_back(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        """A local backfill that has not been published would be deleted with no restore.

        This is the state `publish-lake` leaves an operator in when it refuses because
        the lake moved: the tooling's only other move is to fill, and filling empties
        each table first. Refusing here is what stops "hydrate again" from being the
        instruction that destroys the work.
        """

        store = tmp_path / "ahead.sqlite"
        shutil.copyfile(lake.sqlite_path, store)
        connection = sqlite3.connect(store)
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close, volume) "
            "VALUES ('9984', '2026-03-02', 300.0, 3000.0)"
        )
        connection.commit()
        connection.close()
        before = store.read_bytes()

        with pytest.raises(LakeHydrateError, match="no release can restore"):
            _hydrate(session, lake, store)

        assert store.read_bytes() == before

    def test_filling_allows_a_store_that_is_behind_the_release(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        """Catching up is the ordinary case and must not be caught by the guard above."""

        store = _dehydrated(lake, tmp_path / "behind.sqlite")

        report = _hydrate(session, lake, store)

        assert report.rows == {"jquants.daily_bars": 4, "jquants.short_sale_reports": 2}

    def test_filling_refuses_a_table_that_drifted_from_its_dataset_contract(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        store = _dehydrated(lake, tmp_path / "drifted.sqlite")
        connection = sqlite3.connect(store)
        connection.execute("ALTER TABLE jquants_daily_bars ADD COLUMN settled_at TEXT")
        connection.commit()
        connection.close()

        with pytest.raises(LakeHydrateError, match=r"does not match the jquants\.daily_bars"):
            _hydrate(session, lake, store)

    def test_a_fill_that_loaded_fewer_rows_than_the_release_published_is_refused(
        self, session: LakeSession, lake: Lake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A silently short load is the failure the whole cutover has to fail closed on.

        Screening reading a store whose bars stopped halfway publishes a smaller
        universe as a healthy result, and nothing downstream can tell that apart from a
        quiet market.
        """

        store = _dehydrated(lake, tmp_path / "short.sqlite")
        real = hydrate_module.load_dataset_rows

        def short_load(connection: Any, **kwargs: Any) -> int:
            loaded = real(connection, **kwargs)
            if kwargs["dataset"].name == "jquants.daily_bars":
                connection.execute("DELETE FROM jquants_daily_bars WHERE traded_at = '2026-02-02'")
                return loaded - 1
            return loaded

        monkeypatch.setattr(hydrate_module, "load_dataset_rows", short_load)
        with pytest.raises(LakeHydrateError, match="published 4"):
            _hydrate(session, lake, store)
        # The refusal has to leave the store as it was, not half-filled.
        arrived = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        try:
            assert arrived.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0] == 0
        finally:
            arrived.close()

    def test_a_release_superseded_while_the_fill_waited_is_refused(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        store = _dehydrated(lake, tmp_path / "raced.sqlite")
        moved = ("release-two", "0" * 64)
        with pytest.raises(LakeHydrateError, match="no longer current"):
            _hydrate(session, lake, store, still_current=lambda: moved)

    def test_a_case_insensitive_filesystem_is_rejected_before_the_store_changes(
        self,
        session: LakeSession,
        lake: Lake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = _dehydrated(lake, tmp_path / "arrived.sqlite")
        before = store.read_bytes()
        monkeypatch.setattr(Path, "samefile", lambda _self, _other: True)

        with pytest.raises(LakeHydrateError, match="case-sensitive"):
            _hydrate(session, lake, store)

        assert store.read_bytes() == before
        assert not [path for path in tmp_path.iterdir() if path.name.endswith(".probe")]

    def test_insufficient_capacity_fails_before_the_store_changes(
        self,
        session: LakeSession,
        lake: Lake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = _dehydrated(lake, tmp_path / "arrived.sqlite")
        before = store.read_bytes()
        monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))

        with pytest.raises(LakeHydrateError, match="free bytes"):
            _hydrate(session, lake, store)

        assert store.read_bytes() == before
        assert not [path for path in tmp_path.iterdir() if path.name.endswith(".hydrating")]
        assert not [path for path in tmp_path.iterdir() if path.name.endswith(".rollback")]

    def test_a_failed_atomic_replace_leaves_the_previous_store(
        self,
        session: LakeSession,
        lake: Lake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = _dehydrated(lake, tmp_path / "arrived.sqlite")
        before = store.read_bytes()
        real_replace = Path.replace

        def fail_replace(source: Path, target: Path) -> Path:
            if source.name.endswith(".hydrating"):
                raise OSError("simulated replace failure")
            return real_replace(source, target)

        monkeypatch.setattr(Path, "replace", fail_replace)

        with pytest.raises(OSError, match="replace failure"):
            _hydrate(session, lake, store)

        assert store.read_bytes() == before
        assert not [path for path in tmp_path.iterdir() if path.name.endswith(".hydrating")]
        assert not [path for path in tmp_path.iterdir() if path.name.endswith(".rollback")]

    def test_a_post_replace_directory_fsync_failure_restores_the_previous_store(
        self,
        session: LakeSession,
        lake: Lake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = _dehydrated(lake, tmp_path / "arrived.sqlite")
        before = store.read_bytes()
        real_fsync = os.fsync
        real_replace = Path.replace
        candidate_visible = False
        injected = False

        def record_candidate_replace(source: Path, target: Path) -> Path:
            nonlocal candidate_visible
            result = real_replace(source, target)
            if source.name.endswith(".hydrating"):
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

        with pytest.raises(LakeHydrateError, match="previous generation was restored"):
            _hydrate(session, lake, store)

        assert store.read_bytes() == before
        assert not [path for path in tmp_path.iterdir() if path.name.endswith(".rollback")]

    def test_a_file_that_is_not_a_market_store_is_never_filled(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        """A mistyped store path must not destroy what it points at."""

        for name, payload in (
            ("notes.yaml", b"asof: 2026-08-12\n"),
            ("object.parquet", b"PAR1nonsense"),
            (".env", b"R2_ACCESS_KEY_ID=x\n"),
        ):
            store = tmp_path / name
            store.write_bytes(payload)

            with pytest.raises(sqlite3.DatabaseError, match="not a database"):
                _hydrate(session, lake, store)

            assert store.read_bytes() == payload

    def test_another_stores_database_is_never_filled(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        """A readable SQLite is the mistyped path that a file-type check would miss.

        `runs.sqlite` and the application store open cleanly and pass every integrity
        check, so what refuses them is the contract comparison: the release's tables are
        not there. The copy is made first, so the proof that matters is that the named
        file still holds its own bytes afterwards.
        """

        store = tmp_path / "runs.sqlite"
        connection = sqlite3.connect(store)
        connection.execute("CREATE TABLE screening_run(run_revision_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO screening_run VALUES ('r1')")
        connection.commit()
        connection.close()
        before = store.read_bytes()

        with pytest.raises(LakeHydrateError, match="does not carry"):
            _hydrate(session, lake, store)

        assert store.read_bytes() == before
        assert not [path for path in tmp_path.iterdir() if path.name.endswith(".hydrating")]

    def test_a_store_that_does_not_exist_is_named_rather_than_created(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        with pytest.raises(LakeHydrateError, match="market store does not exist"):
            _hydrate(session, lake, tmp_path / "absent.sqlite")


class TestDehydrate:
    def test_emptying_refuses_when_current_moves_before_delete(
        self, lake: Lake, tmp_path: Path
    ) -> None:
        store = tmp_path / "moved-current.sqlite"
        shutil.copyfile(lake.sqlite_path, store)
        before = store.read_bytes()
        release = resolve_current_release(_cache(lake).source)

        with pytest.raises(LakeHydrateError, match="no longer current"):
            dehydrate_market_store(
                store,
                release=release,
                still_current=lambda: ("new-release", "d" * 64),
            )

        assert store.read_bytes() == before

    def test_emptying_refuses_a_different_embedded_release_before_delete(
        self, lake: Lake, tmp_path: Path
    ) -> None:
        store = tmp_path / "wrong-origin.sqlite"
        shutil.copyfile(lake.sqlite_path, store)
        connection = open_connection(store)
        write_lake_store_origin(
            connection,
            LakeStoreOrigin(
                release_id="different-release",
                release_manifest_sha256="d" * 64,
            ),
        )
        connection.commit()
        connection.close()
        before = store.read_bytes()
        release = resolve_current_release(_cache(lake).source)

        with pytest.raises(LakeHydrateError, match="origin does not match"):
            dehydrate_market_store(store, release=release)

        assert store.read_bytes() == before

    def test_emptying_removes_exactly_what_the_release_publishes(
        self, lake: Lake, tmp_path: Path
    ) -> None:
        store = tmp_path / "leaving.sqlite"
        shutil.copyfile(lake.sqlite_path, store)
        release = resolve_current_release(_cache(lake).source)

        report = dehydrate_market_store(store, release=release)

        assert report.removed_rows == {"jquants.daily_bars": 4, "jquants.short_sale_reports": 2}
        emptied = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        try:
            assert emptied.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone()[0] == 0
            assert emptied.execute("SELECT COUNT(*) FROM source_coverage").fetchone()[0] == 1
        finally:
            emptied.close()

    def test_emptying_refuses_rows_the_release_has_not_published_yet(
        self, lake: Lake, tmp_path: Path
    ) -> None:
        store = tmp_path / "ahead.sqlite"
        shutil.copyfile(lake.sqlite_path, store)
        connection = sqlite3.connect(store)
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close, volume) "
            "VALUES ('9984', '2026-03-02', 300.0, 3000.0)"
        )
        connection.commit()
        connection.close()
        release = resolve_current_release(_cache(lake).source)

        with pytest.raises(LakeHydrateError, match="publish before emptying"):
            dehydrate_market_store(store, release=release)

    def test_emptying_refuses_a_dataset_the_release_does_not_carry_at_all(
        self, lake: Lake, tmp_path: Path
    ) -> None:
        """A declared dataset whose source has not started publishing still holds rows.

        Emptying it because the registry names it would drop the first rows a new source
        ever produced, with no release able to give them back.
        """

        store = tmp_path / "unpublished.sqlite"
        shutil.copyfile(lake.sqlite_path, store)
        connection = sqlite3.connect(store)
        connection.execute(
            "INSERT INTO jquants_market_calendar(day, is_business_day) VALUES ('2026-03-02', 1)"
        )
        connection.commit()
        connection.close()
        release = resolve_current_release(_cache(lake).source)

        with pytest.raises(LakeHydrateError, match="the release does not publish"):
            dehydrate_market_store(store, release=release)


def test_v23_store_migrates_hydrates_exports_and_dehydrates_as_one_cutover(
    session: LakeSession, lake: Lake, tmp_path: Path
) -> None:
    store = tmp_path / "cutover.sqlite"
    shutil.copyfile(lake.sqlite_path, store)
    with sqlite3.connect(store) as connection:
        connection.execute("DROP TABLE lake_store_origin")
        connection.execute("PRAGMA user_version = 23")

    migrated = open_connection(store)
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == SQLITE_SCHEMA_VERSION == 24
    assert migrated.execute("SELECT COUNT(*) FROM lake_store_origin").fetchone()[0] == 0
    migrated.close()

    source = LocalMirrorSource(lake.mirror)
    current = resolve_current_release(source)
    hydrate_market_store(
        session,
        release=current,
        cache=LakeObjectCache(root=tmp_path / "cutover-cache", source=source),
        store=store,
        dataset_names=current.dataset_names(),
    )
    assert read_lake_store_origin(store) == LakeStoreOrigin(
        release_id=current.release_id,
        release_manifest_sha256=current.manifest_sha256,
    )

    candidate_mirror = tmp_path / "candidate"
    export = export_lake_legacy(
        sqlite_path=store,
        mirror_root=candidate_mirror,
        producer_git_commit=_COMMIT,
        expected_store_origin=read_lake_store_origin(store),
        created_at=_BUILT_AT,
    )
    candidate_path, candidate_model = create_l1_release(
        dataset_manifest_paths=[item.manifest_path for item in export.datasets.values()],
        mirror_root=candidate_mirror,
        release_id="cutover-candidate",
        created_at=_BUILT_AT,
    )
    candidate = resolve_release(
        LocalMirrorSource(candidate_mirror),
        candidate_model.release_id,
        manifest_sha256=sha256_file(candidate_path),
    )
    advance_lake_store_origin(
        store,
        expected=read_lake_store_origin(store),
        target=LakeStoreOrigin(
            release_id=candidate.release_id,
            release_manifest_sha256=candidate.manifest_sha256,
        ),
    )

    report = dehydrate_market_store(store, release=candidate)

    assert report.removed_rows == {"jquants.daily_bars": 4, "jquants.short_sale_reports": 2}
    with sqlite3.connect(f"{store.resolve().as_uri()}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 24
        assert connection.execute("SELECT COUNT(*) FROM lake_store_origin").fetchone()[0] == 1


class TestOperatorDerivedRetraction:
    """A derivation that drops a row must not have it come back through the lake.

    `jpx_delistings` and `tender_offer_exit_values` were kept out of the lake precisely
    because a key-wise merge would reinstate a row a later derivation retracted — an
    offer that turned out to have a second bidder, a company the exchange removed from
    its list — and put a price into the calibration forward that the current rules say
    cannot be established. Lake ownership answers that rather than working around it:
    hydrate empties the table before it fills, so absence in the newer generation is
    what the store ends up holding. This is the test that says so.
    """

    @pytest.fixture(autouse=True)
    def _policy(self, _small_pilot_release_policy: None, monkeypatch: pytest.MonkeyPatch) -> None:
        # Requested by name so it runs after the module-wide narrowing rather than
        # before it; autouse order alone would leave this dataset outside the profile.
        datasets = tuple(
            item.model_copy(
                update={
                    "coverage_start_on_or_before": date.max,
                    "minimum_rows": 1,
                    "minimum_population_count": 1,
                    "max_age_days": 3660,
                    "max_lead_days": 3660,
                }
            )
            for item in _FULL_RELEASE_POLICY.datasets
            if item.dataset == "edinet.tender_offer_exit_values"
        )
        monkeypatch.setattr(
            lake_models,
            "PRODUCTION_RELEASE_POLICY",
            _FULL_RELEASE_POLICY.model_copy(update={"datasets": datasets}),
        )

    @staticmethod
    def _store(path: Path, tickers: tuple[str, ...]) -> Path:
        connection = open_connection(path)
        connection.executemany(
            "INSERT OR REPLACE INTO tender_offer_exit_values("
            "ticker, delisted_on, offer_price_yen, offer_doc_id, result_doc_id, filed_on"
            ") VALUES (?, '2026-05-01', 1060.0, 'REG', 'RES', '2026-02-01')",
            [(ticker,) for ticker in tickers],
        )
        connection.commit()
        connection.close()
        return path

    def _release(self, sqlite_path: Path, mirror: Path, release_id: str) -> str:
        with sealed_sqlite_snapshot(
            sqlite_path=sqlite_path, mirror_root=mirror, snapshot_id=f"snapshot-{release_id}"
        ) as snapshot:
            manifest_path = export_legacy_sqlite(
                dataset_name="edinet.tender_offer_exit_values",
                mirror_root=mirror,
                producer_git_commit=_COMMIT,
                source_snapshot=snapshot,
                build_id=f"build-exits-{release_id}",
                created_at=_BUILT_AT,
            ).manifest_path
        release_manifest, _release = create_l1_release(
            dataset_manifest_paths=[manifest_path],
            mirror_root=mirror,
            release_id=release_id,
            created_at=_BUILT_AT,
        )
        _publish_pointer(mirror, release_id, release_manifest)
        connection = open_connection(sqlite_path)
        write_lake_store_origin(
            connection,
            LakeStoreOrigin(
                release_id=release_id,
                release_manifest_sha256=_release_digest(mirror, release_id),
            ),
        )
        connection.commit()
        connection.close()
        return release_id

    def test_a_retracted_exit_value_does_not_come_back_through_hydrate(
        self, session: LakeSession, tmp_path: Path
    ) -> None:
        """The newer generation is what the store ends up holding, row for row."""

        mirror = tmp_path / "mirror"
        first_source = self._store(tmp_path / "gen1.sqlite", ("2000", "3000"))
        self._release(first_source, mirror, "release-gen1")
        second_source = self._store(tmp_path / "gen2.sqlite", ("2000",))
        self._release(second_source, mirror, "release-gen2")

        # The store arrives dehydrated, which is the shape both the daily batch and
        # `hydrate-market` fill: `push-market` empties every lake-owned table before it
        # uploads, so the published copy has nothing to put a retracted row back from.
        store = self._store(tmp_path / "store.sqlite", ())
        release = resolve_release(
            LocalMirrorSource(mirror),
            "release-gen2",
            manifest_sha256=_release_digest(mirror, "release-gen2"),
        )
        hydrate_market_store(
            session,
            release=release,
            cache=LakeObjectCache(root=tmp_path / "cache", source=LocalMirrorSource(mirror)),
            store=store,
            dataset_names=("edinet.tender_offer_exit_values",),
        )

        connection = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        try:
            held = {
                str(row[0])
                for row in connection.execute("SELECT ticker FROM tender_offer_exit_values")
            }
        finally:
            connection.close()
        assert held == {"2000"}

    def test_the_published_copy_carries_no_row_to_resurrect(self, tmp_path: Path) -> None:
        """The resurrection vector is gone rather than guarded against.

        `push-market` dehydrates every lake-owned table before it uploads, so the copy a
        later merge would read holds none of these rows at all. This is what replaced the
        key-wise merge exemption these two tables used to need.
        """

        mirror = tmp_path / "mirror"
        source = self._store(tmp_path / "gen1.sqlite", ("2000", "3000"))
        self._release(source, mirror, "release-push")
        release = resolve_release(
            LocalMirrorSource(mirror),
            "release-push",
            manifest_sha256=_release_digest(mirror, "release-push"),
        )

        report = dehydrate_market_store(source, release=release)

        assert report.removed_rows["edinet.tender_offer_exit_values"] == 2
        connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            held = connection.execute("SELECT count(*) FROM tender_offer_exit_values").fetchone()[0]
        finally:
            connection.close()
        assert held == 0

    def test_the_older_generation_still_holds_the_row_it_named(
        self, session: LakeSession, tmp_path: Path
    ) -> None:
        """Retraction is a property of the newer generation, not a deletion of history.

        Without this, a hydrate that silently produced an empty table would pass the
        test above for the wrong reason.
        """

        mirror = tmp_path / "mirror"
        self._release(self._store(tmp_path / "gen1.sqlite", ("2000", "3000")), mirror, "gen-one")
        store = self._store(tmp_path / "store.sqlite", ())
        release = resolve_release(
            LocalMirrorSource(mirror),
            "gen-one",
            manifest_sha256=_release_digest(mirror, "gen-one"),
        )
        hydrate_market_store(
            session,
            release=release,
            cache=LakeObjectCache(root=tmp_path / "cache", source=LocalMirrorSource(mirror)),
            store=store,
            dataset_names=("edinet.tender_offer_exit_values",),
        )

        connection = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        try:
            held = {
                str(row[0])
                for row in connection.execute("SELECT ticker FROM tender_offer_exit_values")
            }
        finally:
            connection.close()
        assert held == {"2000", "3000"}


@dataclass
class _CorruptingSource:
    """A source that flips one byte of one key's payload, length preserved."""

    inner: LocalMirrorSource
    key: str

    def read_bytes(self, key: str) -> bytes:
        payload = self.inner.read_bytes(key)
        if key != self.key:
            return payload
        return payload[:-1] + bytes([payload[-1] ^ 1])

    def uri(self, key: str) -> str:
        return self.inner.uri(key)


@dataclass
class _RefusingSource:
    """A source whose every object read fails, as a worker's session might."""

    inner: LocalMirrorSource

    def read_bytes(self, key: str) -> bytes:
        raise LakeObjectError(f"worker cannot read: {key}")

    def uri(self, key: str) -> str:
        return self.inner.uri(key)


class TestPrefetchingHydration:
    """The prefetching source must change wall time only, never a fill's meaning."""

    _NAMES = ("jquants.daily_bars", "jquants.short_sale_reports")

    def _release(self, lake: Lake) -> Any:
        return resolve_current_release(LocalMirrorSource(lake.mirror))

    def test_planned_order_is_exactly_the_order_a_fill_reads_objects(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        recording = RecordingSource(LocalMirrorSource(lake.mirror))
        cache = LakeObjectCache(root=tmp_path / "cache", source=recording)
        release = self._release(lake)
        planned = hydration_order(release, self._NAMES)

        hydrate_market_store(
            session,
            release=release,
            cache=cache,
            store=_dehydrated(lake, tmp_path / "arrived.sqlite"),
            dataset_names=self._NAMES,
        )

        planned_keys = {item.key for item in planned}
        assert [key for key in recording.reads if key in planned_keys] == [
            item.key for item in planned
        ]

    def test_prefetched_fill_reads_objects_from_workers_and_counts_them_fetched(
        self, session: LakeSession, lake: Lake, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        base = RecordingSource(LocalMirrorSource(lake.mirror))
        workers = RecordingSource(LocalMirrorSource(lake.mirror))
        release = self._release(lake)
        planned = hydration_order(release, self._NAMES)
        source = PrefetchingSource(
            base=base,
            objects=planned,
            source_factory=lambda: nullcontext(workers),
            mirror=tmp_path / "cache",
        )
        try:
            report = hydrate_market_store(
                session,
                release=release,
                cache=LakeObjectCache(root=tmp_path / "cache", source=source),
                store=_dehydrated(lake, tmp_path / "arrived.sqlite"),
                dataset_names=self._NAMES,
            )
        finally:
            source.close()

        assert report.rows == {"jquants.daily_bars": 4, "jquants.short_sale_reports": 2}
        assert sorted(workers.reads) == sorted(item.key for item in planned)
        planned_keys = {item.key for item in planned}
        assert not [key for key in base.reads if key in planned_keys]
        assert report.transfers.fetched_objects == len(planned)
        assert report.transfers.reused_objects == 0
        assert (
            f"served {len(planned)}/{len(planned)} planned objects (0 already mirrored)"
            in capfd.readouterr().err
        )

    def test_a_corrupted_prefetched_payload_fails_the_fill_closed(
        self, session: LakeSession, lake: Lake, tmp_path: Path
    ) -> None:
        release = self._release(lake)
        planned = hydration_order(release, self._NAMES)
        corrupting = _CorruptingSource(LocalMirrorSource(lake.mirror), planned[-1].key)
        source = PrefetchingSource(
            base=LocalMirrorSource(lake.mirror),
            objects=planned,
            source_factory=lambda: nullcontext(corrupting),
            mirror=tmp_path / "cache",
        )
        try:
            with pytest.raises(LakeObjectError, match="digest differs"):
                hydrate_market_store(
                    session,
                    release=release,
                    cache=LakeObjectCache(root=tmp_path / "cache", source=source),
                    store=_dehydrated(lake, tmp_path / "arrived.sqlite"),
                    dataset_names=self._NAMES,
                )
        finally:
            source.close()

    def test_worker_failure_degrades_to_sequential_reads_and_the_fill_completes(
        self, session: LakeSession, lake: Lake, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        base = RecordingSource(LocalMirrorSource(lake.mirror))
        release = self._release(lake)
        planned = hydration_order(release, self._NAMES)
        source = PrefetchingSource(
            base=base,
            objects=planned,
            source_factory=lambda: nullcontext(_RefusingSource(LocalMirrorSource(lake.mirror))),
            mirror=tmp_path / "cache",
        )
        try:
            report = hydrate_market_store(
                session,
                release=release,
                cache=LakeObjectCache(root=tmp_path / "cache", source=source),
                store=_dehydrated(lake, tmp_path / "arrived.sqlite"),
                dataset_names=self._NAMES,
            )
        finally:
            source.close()

        assert report.rows == {"jquants.daily_bars": 4, "jquants.short_sale_reports": 2}
        planned_keys = {item.key for item in planned}
        assert {key for key in base.reads if key in planned_keys} == planned_keys
        assert "prefetch disabled" in capfd.readouterr().err

    def test_an_already_mirrored_object_is_not_fetched_again(
        self, session: LakeSession, lake: Lake, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        cache_root = tmp_path / "cache"
        release = self._release(lake)
        planned = hydration_order(release, self._NAMES)
        LakeObjectCache(root=cache_root, source=LocalMirrorSource(lake.mirror)).materialize(
            planned[0]
        )
        workers = RecordingSource(LocalMirrorSource(lake.mirror))
        source = PrefetchingSource(
            base=LocalMirrorSource(lake.mirror),
            objects=planned,
            source_factory=lambda: nullcontext(workers),
            mirror=cache_root,
        )
        try:
            report = hydrate_market_store(
                session,
                release=release,
                cache=LakeObjectCache(root=cache_root, source=source),
                store=_dehydrated(lake, tmp_path / "arrived.sqlite"),
                dataset_names=self._NAMES,
            )
        finally:
            source.close()

        assert planned[0].key not in workers.reads
        assert report.transfers.reused_objects == 1
        assert report.transfers.fetched_objects == len(planned) - 1
        assert "(1 already mirrored)" in capfd.readouterr().err

    def test_offline_hydration_cache_passes_through_unchanged(self, lake: Lake) -> None:
        cache = _cache(lake)
        with prefetching_hydration_cache(
            cache,
            release=self._release(lake),
            dataset_names=self._NAMES,
            bucket=None,
        ) as out:
            assert out is cache

    def test_a_stalled_worker_fetch_falls_back_to_the_base_read(
        self, lake: Lake, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        release = self._release(lake)
        planned = hydration_order(release, self._NAMES)
        inner = LocalMirrorSource(lake.mirror)
        gate = threading.Event()
        stalled_key = planned[0].key

        @dataclass
        class _Stalling:
            def read_bytes(self, key: str) -> bytes:
                if key == stalled_key:
                    gate.wait()
                return inner.read_bytes(key)

            def uri(self, key: str) -> str:
                return inner.uri(key)

        source = PrefetchingSource(
            base=inner,
            objects=planned,
            source_factory=lambda: nullcontext(_Stalling()),
            mirror=tmp_path / "cache",
            wait_seconds=0.2,
        )
        try:
            assert source.read_bytes(stalled_key) == inner.read_bytes(stalled_key)
        finally:
            gate.set()
            source.close()
        assert "prefetch disabled" in capfd.readouterr().err

    def test_r2_hydration_cache_gives_each_worker_its_own_session(
        self,
        session: LakeSession,
        lake: Lake,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The bucket branch is exercised offline: one session and one source per worker."""

        opened_sessions: list[object] = []
        worker_sources: list[RecordingSource] = []

        @contextmanager
        def fake_lake_session(*, credentials: object) -> Iterator[object]:
            token = object()
            opened_sessions.append(token)
            yield token

        def fake_r2_source(worker_session: object) -> RecordingSource:
            source = RecordingSource(LocalMirrorSource(lake.mirror))
            worker_sources.append(source)
            return source

        monkeypatch.setattr(prefetch_module, "lake_session", fake_lake_session)
        monkeypatch.setattr(prefetch_module, "R2ObjectSource", fake_r2_source)
        monkeypatch.setenv("R2_ACCOUNT_ID", "0" * 32)
        monkeypatch.setenv("R2_ACCESS_KEY_ID", "A" * 20)
        monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "acceptance-dummy")

        cache = LakeObjectCache(root=tmp_path / "cache", source=LocalMirrorSource(lake.mirror))
        release = self._release(lake)
        planned = hydration_order(release, self._NAMES)
        with prefetching_hydration_cache(
            cache,
            release=release,
            dataset_names=self._NAMES,
            bucket="acceptance-fake",
        ) as hydration_cache:
            assert hydration_cache is not cache
            assert hydration_cache.transfers is cache.transfers
            report = hydrate_market_store(
                session,
                release=release,
                cache=hydration_cache,
                store=_dehydrated(lake, tmp_path / "arrived.sqlite"),
                dataset_names=self._NAMES,
            )

        assert report.rows == {"jquants.daily_bars": 4, "jquants.short_sale_reports": 2}
        assert len(opened_sessions) == min(8, len(planned))
        assert len(worker_sources) == len(opened_sessions)
        served = [key for source in worker_sources for key in source.reads]
        assert sorted(served) == sorted(item.key for item in planned)

    def test_a_failing_source_factory_degrades_to_sequential_reads(
        self, lake: Lake, tmp_path: Path, capfd: pytest.CaptureFixture[str]
    ) -> None:
        """A worker that cannot even open its source leaves the fill on the base read."""

        inner = LocalMirrorSource(lake.mirror)
        release = self._release(lake)
        planned = hydration_order(release, self._NAMES)

        def refusing_factory() -> AbstractContextManager[LocalMirrorSource]:
            raise RuntimeError("worker session cannot be opened")

        source = PrefetchingSource(
            base=inner,
            objects=planned,
            source_factory=refusing_factory,
            mirror=tmp_path / "cache",
        )
        try:
            for item in planned:
                assert source.read_bytes(item.key) == inner.read_bytes(item.key)
        finally:
            source.close()
        err = capfd.readouterr().err
        assert "prefetch disabled" in err
        assert f"served 0/{len(planned)} planned objects" in err
