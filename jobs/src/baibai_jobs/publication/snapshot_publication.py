"""Export the market store into the lake, seal a release, and publish it to R2.

This is the write half of the daily cutover. Its read half is ``lake hydrate``, and the
two are joined by one rule: the serving release must still be the one the store was
filled from. That identity lives in the SQLite file itself: a sidecar can be restored or
copied independently and therefore cannot prove which rows the export is reading.

Native prices and master snapshots use this execution's rebuilt projection and fixed L1
identity. Other datasets remain on the legacy store path during migration. The serving
release is read for two facts only — the origin the store must be bound to, and the
history floor the new release must still reach — and none of its partitions are carried.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from baibai_engine.data_collection.providers.jquants.calendar_native import (
    CalendarL1,
    CalendarObjectStore,
)
from baibai_engine.data_collection.providers.jquants.full_day_native import FullDayObjectStore
from baibai_engine.data_collection.providers.jquants.margin_alerts_native import MarginAlertsL1
from baibai_engine.data_collection.providers.jquants.margin_interest_native import MarginInterestL1
from baibai_engine.data_collection.providers.jquants.master_native import (
    MasterL1,
    MasterObjectStore,
)
from baibai_engine.data_collection.providers.jquants.price_native import PriceL1, PriceObjectStore
from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
    ShortSaleReportsL1,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.edinet_inputs import read_edinet_input
from baibai_engine.storage.filesystem import fsync_directory
from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError, ObjectStore
from baibai_engine.storage.snapshots.identity import verified_git_commit as lake_verified_git_commit
from baibai_engine.storage.snapshots.keys import dataset_manifest_key as lake_dataset_manifest_key
from baibai_engine.storage.snapshots.models import ReleaseManifest as LakeReleaseManifest
from baibai_engine.storage.snapshots.models import (
    canonical_lake_model_bytes,
    load_lake_model_json,
)
from baibai_engine.storage.snapshots.objects import LocalMirrorSource
from baibai_engine.storage.snapshots.objects import mirror_path as lake_mirror_path
from baibai_engine.storage.snapshots.reader import FixedRelease as LakeFixedRelease
from baibai_engine.storage.snapshots.reader import (
    LakeReadError,
    resolve_release,
    selected_partitions,
    verify_object,
)
from baibai_engine.storage.snapshots.release import LakeReleasePointer
from baibai_engine.storage.snapshots.release import create_release as create_lake_release
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.snapshots.writer import (
    LakeBuildError,
    LakeBuildReport,
    LakeExportReport,
    SealedSQLiteSnapshot,
    export_market_snapshot,
    sealed_sqlite_snapshot,
)
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    LakeStoreOriginError,
    advance_lake_store_origin,
    read_lake_store_origin,
)
from baibai_jobs.integration.calendar_native import model_input as native_calendar_model_input
from baibai_jobs.integration.margin_alerts_native import model_input as native_alerts_model_input
from baibai_jobs.integration.margin_interest_native import (
    model_input as native_interest_model_input,
)
from baibai_jobs.integration.master_native import model_input as native_master_model_input
from baibai_jobs.integration.price_native import model_input as native_price_model_input
from baibai_jobs.integration.short_sale_reports_native import (
    model_input as native_shorts_model_input,
)
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.l1_calendar_export import export_calendar_from_native_input
from baibai_jobs.publication.l1_margin_alerts_export import export_margin_alerts_from_native_input
from baibai_jobs.publication.l1_margin_interest_export import (
    export_margin_interest_from_native_input,
)
from baibai_jobs.publication.l1_master_export import export_master_from_native_input
from baibai_jobs.publication.l1_price_export import export_prices_from_native_input
from baibai_jobs.publication.l1_short_sale_reports_export import (
    export_short_sale_reports_from_native_input,
)
from baibai_jobs.publication.lease import LeaseError, _load_handle, verify_held
from baibai_jobs.publication.policy import release_policy_for_profile
from baibai_jobs.publication.snapshot_release import (
    PointerSnapshot,
    TransferReport,
    publish_release,
    read_pointer_snapshot,
)


def _phase_progress(phase: str, started: float | None = None) -> float:
    now = time.perf_counter()
    state = "start" if started is None else f"done elapsed_seconds={now - started:.3f}"
    print(f"market lake: phase={phase} state={state}", file=sys.stderr, flush=True)
    return now


@dataclass(frozen=True, slots=True)
class MarketLakePublishReport:
    release_id: str
    release_manifest_sha256: str
    data_as_of: str
    previous_release_id: str | None
    changed_partitions: Mapping[str, int]
    """Per dataset, how many partitions landed on a content-addressed key the mirror
    did not hold — the partitions whose bytes moved since the previous publication."""
    uploaded_objects: int
    uploaded_bytes: int
    transfers: TransferReport

    def as_dict(self) -> dict[str, object]:
        return {
            "changed_partitions": dict(sorted(self.changed_partitions.items())),
            "data_as_of": self.data_as_of,
            "previous_release_id": self.previous_release_id,
            "release_id": self.release_id,
            "release_manifest_sha256": self.release_manifest_sha256,
            "uploaded_bytes": self.uploaded_bytes,
            "uploaded_objects": self.uploaded_objects,
            "publication_transfers": self.transfers.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class PreparedMarketLake:
    """生成と公開が共有する一回のseal。prepareのcontext内だけで使用する。"""

    sqlite_path: Path
    mirror_root: Path
    source_snapshot: SealedSQLiteSnapshot
    serving: PointerSnapshot
    export: LakeExportReport
    release_manifest_path: Path
    release: LakeReleaseManifest
    release_sha256: str
    base_resolve_seconds: float
    seal_export_seconds: float
    release_create_seconds: float
    price_identity: InputSnapshot | None = None
    master_identity: InputSnapshot | None = None
    calendar_identity: InputSnapshot | None = None
    margin_alerts_identity: InputSnapshot | None = None
    margin_interest_identity: InputSnapshot | None = None
    short_sale_reports_identity: InputSnapshot | None = None


@contextmanager
def prepare_market_lake(
    *,
    sqlite_path: Path,
    mirror_root: Path,
    store: ObjectStore,
    release_id: str | None = None,
    as_of: date | None = None,
    edinet_input: InputSnapshot | None = None,
) -> Iterator[PreparedMarketLake]:
    """一回のseal/exportから新releaseを固定する。公開pointer/originは動かさない。"""
    fixed_edinet = read_edinet_input(store, edinet_input) if edinet_input is not None else None
    policy = release_policy_for_profile("production")
    policy_names = {item.dataset for item in policy.datasets}
    native_alerts = "jquants.margin_alerts" in policy_names
    native_shorts = "jquants.short_sale_reports" in policy_names
    native_interest = bool(
        {"jquants.weekly_margin", "jquants.all_issues_daily_margin"} & policy_names
    )
    phase_started = _phase_progress("native_calendar")
    fixed_calendar = CalendarL1(
        cast(CalendarObjectStore, store), mirror_root / "calendar-native"
    ).fixed()
    evaluated_at = datetime.now(UTC)
    calendar_identity = native_calendar_model_input(
        store,
        fixed_calendar,
        as_of=as_of or evaluated_at.date(),
        evaluated_at=evaluated_at,
        market=sqlite_path,
    )
    _phase_progress("native_calendar", phase_started)
    phase_started = _phase_progress("native_master")
    fixed_master = MasterL1(cast(MasterObjectStore, store), mirror_root / "master-native").fixed()
    evaluated_at = datetime.now(UTC)
    master_identity = native_master_model_input(
        store,
        fixed_master,
        as_of=as_of or evaluated_at.date(),
        evaluated_at=evaluated_at,
        market=sqlite_path,
    )
    _phase_progress("native_master", phase_started)
    phase_started = _phase_progress("native_price")
    fixed = PriceL1(cast(PriceObjectStore, store), mirror_root / "price-native").fixed()
    evaluated_at = datetime.now(UTC)
    price_identity = native_price_model_input(
        store,
        fixed,
        as_of=as_of or evaluated_at.date(),
        evaluated_at=evaluated_at,
        market=sqlite_path,
        calendar_input=calendar_identity,
    )
    _phase_progress("native_price", phase_started)
    started = time.perf_counter()
    phase_started = _phase_progress("base_resolve")
    serving = _serving_pointer(store)
    previous = serving.pointer
    published_coverage_start: dict[str, date] = {}
    serving_release = None
    if previous is not None:
        serving_release = _resolve_serving_release(store, mirror_root, previous)
        published_coverage_start = {
            name: manifest.coverage_start
            for name, manifest in serving_release.dataset_manifests.items()
            if name
            not in {
                "jquants.fin_summaries",
                "jquants.daily_bars",
                "jquants.master_snapshots",
                "jquants.market_calendar",
            }
            and name != "edinet.list_responses"
            and not (native_alerts and name == "jquants.margin_alerts")
            and not (native_shorts and name == "jquants.short_sale_reports")
            and not (
                native_interest
                and name in {"jquants.weekly_margin", "jquants.all_issues_daily_margin"}
            )
        }
    _phase_progress("base_resolve", phase_started)
    if fixed_edinet is not None:
        from baibai_jobs.integration.edinet_archive_native import prepare_archive_execution

        if read_lake_store_origin(sqlite_path) != _pointer_origin(previous):
            raise LakePublishError("fixed EDINET publication requires the serving store origin")
        phase_started = _phase_progress("retain_edinet_archives")
        prepare_archive_execution(fixed_edinet.archives, sqlite_path)
        published_coverage_start.pop("edinet.source_archives", None)
        _phase_progress("retain_edinet_archives", phase_started)
    alerts_identity = None
    if native_alerts:
        phase_started = _phase_progress("native_margin_alerts")
        previous_alerts = None
        if serving_release is not None and read_lake_store_origin(sqlite_path) == _pointer_origin(
            previous
        ):
            previous_alerts = serving_release.dataset_manifests.get("jquants.margin_alerts")
        fixed_alerts = MarginAlertsL1(
            cast(FullDayObjectStore, store), mirror_root / "margin-alerts-native"
        ).fixed()
        evaluated_at = datetime.now(UTC)
        alerts_identity = native_alerts_model_input(
            store,
            fixed_alerts,
            as_of=as_of or evaluated_at.date(),
            evaluated_at=evaluated_at,
            market=sqlite_path,
            mirror_root=mirror_root,
            previous=previous_alerts,
        )
        _phase_progress("native_margin_alerts", phase_started)
    shorts_identity = None
    if native_shorts:
        phase_started = _phase_progress("native_short_sale_reports")
        previous_shorts = None
        if serving_release is not None and read_lake_store_origin(sqlite_path) == _pointer_origin(
            previous
        ):
            previous_shorts = serving_release.dataset_manifests.get("jquants.short_sale_reports")
        fixed_shorts = ShortSaleReportsL1(
            cast(FullDayObjectStore, store), mirror_root / "short-sale-reports-native"
        ).fixed()
        evaluated_at = datetime.now(UTC)
        shorts_identity = native_shorts_model_input(
            store,
            fixed_shorts,
            as_of=as_of or evaluated_at.date(),
            evaluated_at=evaluated_at,
            market=sqlite_path,
            mirror_root=mirror_root,
            previous=previous_shorts,
        )
        _phase_progress("native_short_sale_reports", phase_started)
    interest_identity = None
    if native_interest:
        phase_started = _phase_progress("native_margin_interest")
        previous_interest = None
        if serving_release is not None and read_lake_store_origin(sqlite_path) == _pointer_origin(
            previous
        ):
            previous_interest = dict(serving_release.dataset_manifests)
        fixed_interest = MarginInterestL1(
            cast(FullDayObjectStore, store), mirror_root / "margin-interest-native"
        ).fixed()
        evaluated_at = datetime.now(UTC)
        interest_identity = native_interest_model_input(
            store,
            fixed_interest,
            fixed_calendar,
            as_of=as_of or evaluated_at.date(),
            evaluated_at=evaluated_at,
            market=sqlite_path,
            mirror_root=mirror_root,
            previous=previous_interest,
        )
        _phase_progress("native_margin_interest", phase_started)
    base_finished = time.perf_counter()
    with sealed_sqlite_snapshot(sqlite_path=sqlite_path, mirror_root=mirror_root) as snapshot:
        native_calendar_enabled = "jquants.market_calendar" in policy_names
        native_master_enabled = "jquants.master_snapshots" in policy_names
        native_price_enabled = "jquants.daily_bars" in policy_names
        native_alerts_enabled = native_alerts and "jquants.margin_alerts" in policy_names
        native_shorts_enabled = native_shorts and "jquants.short_sale_reports" in policy_names
        native_interest_enabled = (
            native_interest
            and {"jquants.weekly_margin", "jquants.all_issues_daily_margin"} <= policy_names
        )
        derived_names = {
            name
            for name, enabled in (
                ("jquants.market_calendar", native_calendar_enabled),
                ("jquants.margin_alerts", native_alerts_enabled),
                ("jquants.short_sale_reports", native_shorts_enabled),
                ("jquants.weekly_margin", native_interest_enabled),
                ("jquants.all_issues_daily_margin", native_interest_enabled),
                ("jquants.master_snapshots", native_master_enabled),
                ("jquants.daily_bars", native_price_enabled),
            )
            if enabled
        }
        catalog = {
            name: item
            for name, item in LAKE_DATASETS.items()
            if name not in derived_names
            and name != "jquants.fin_summaries"
            and name != "jquants.daily_bars"
            and name != "jquants.master_snapshots"
            and name != "jquants.market_calendar"
            and name != "edinet.list_responses"
            and not (fixed_edinet is not None and name == "edinet.source_archives")
        }
        git_commit = lake_verified_git_commit()
        export = export_market_snapshot(
            source_snapshot=snapshot,
            mirror_root=mirror_root,
            producer_git_commit=git_commit,
            expected_store_origin=_pointer_origin(previous),
            catalog=catalog,
            previous=serving_release.dataset_manifests if serving_release is not None else None,
            allow_empty=native_calendar_enabled
            or native_master_enabled
            or native_price_enabled
            or native_alerts_enabled
            or native_shorts_enabled
            or native_interest_enabled,
        )
        if derived_names:
            datasets = dict(export.datasets)
            if native_price_enabled:
                datasets["jquants.daily_bars"] = export_prices_from_native_input(
                    source_snapshot=snapshot,
                    saved=price_identity,
                    mirror_root=mirror_root,
                    producer_git_commit=git_commit,
                )
            if native_calendar_enabled:
                datasets["jquants.market_calendar"] = export_calendar_from_native_input(
                    source_snapshot=snapshot,
                    saved=calendar_identity,
                    mirror_root=mirror_root,
                    producer_git_commit=git_commit,
                )
            if native_master_enabled:
                datasets["jquants.master_snapshots"] = export_master_from_native_input(
                    source_snapshot=snapshot,
                    saved=master_identity,
                    mirror_root=mirror_root,
                    producer_git_commit=git_commit,
                )
            if native_alerts_enabled:
                assert alerts_identity is not None
                datasets["jquants.margin_alerts"] = export_margin_alerts_from_native_input(
                    source_snapshot=snapshot,
                    saved=alerts_identity,
                    mirror_root=mirror_root,
                    producer_git_commit=git_commit,
                )
            if native_shorts_enabled:
                assert shorts_identity is not None
                datasets["jquants.short_sale_reports"] = (
                    export_short_sale_reports_from_native_input(
                        source_snapshot=snapshot,
                        saved=shorts_identity,
                        mirror_root=mirror_root,
                        producer_git_commit=git_commit,
                    )
                )
            if native_interest_enabled:
                assert interest_identity is not None
                datasets.update(
                    export_margin_interest_from_native_input(
                        source_snapshot=snapshot,
                        saved=interest_identity,
                        mirror_root=mirror_root,
                        producer_git_commit=git_commit,
                    )
                )
            export = replace(export, datasets=datasets)
        phase_started = _phase_progress("release_prepare")
        # source_pages mixes native-migrated originals with sources still served here.
        # Compare like-for-like history, without exempting the remaining originals.
        migrated_sources = frozenset(
            source
            for source, enabled in (
                ("daily_bars", True),
                ("master", True),
                ("market_calendar", True),
                ("margin_alerts", native_alerts),
                ("short_sale_reports", native_shorts),
                ("margin_interest", native_interest),
            )
            if enabled
        )
        source_pages = export.datasets.get("jquants.source_pages")
        served_start = published_coverage_start.get("jquants.source_pages")
        if (
            migrated_sources
            and serving_release is not None
            and served_start is not None
            and (source_pages is None or source_pages.manifest.coverage_start > served_start)
        ):
            remaining_start = _remaining_source_pages_start(
                serving_release, store, mirror_root, migrated_sources
            )
            if remaining_start is None:
                published_coverage_start.pop("jquants.source_pages")
            else:
                published_coverage_start["jquants.source_pages"] = remaining_start
        export_finished = time.perf_counter()
        with _export_manifest_snapshot(export.datasets, mirror_root) as manifest_paths:
            release_path, release = create_lake_release(
                published_coverage_start=published_coverage_start,
                dataset_manifest_paths=manifest_paths,
                mirror_root=mirror_root,
                release_id=release_id,
                catalog=LAKE_DATASETS,
                policy=release_policy_for_profile("production"),
            )
            _phase_progress("release_prepare", phase_started)
            yield PreparedMarketLake(
                sqlite_path=sqlite_path,
                mirror_root=mirror_root,
                source_snapshot=snapshot,
                serving=serving,
                export=export,
                release_manifest_path=release_path,
                release=release,
                release_sha256=hashlib.sha256(canonical_lake_model_bytes(release)).hexdigest(),
                base_resolve_seconds=base_finished - started,
                seal_export_seconds=export_finished - base_finished,
                release_create_seconds=time.perf_counter() - export_finished,
                price_identity=price_identity,
                master_identity=master_identity,
                calendar_identity=calendar_identity,
                margin_alerts_identity=alerts_identity,
                margin_interest_identity=interest_identity,
                short_sale_reports_identity=shorts_identity,
            )


def publish_prepared_market_lake(
    prepared: PreparedMarketLake,
    *,
    store: ObjectStore,
    before_pointer_switch: Callable[[], None] | None = None,
    verified_readback_root: Path | None = None,
) -> MarketLakePublishReport:
    """生成に使用したreleaseをそのまま公開する。export/createをやり直さない。"""
    published = publish_release(
        mirror_root=prepared.mirror_root,
        release_manifest_path=prepared.release_manifest_path,
        expected_release_sha256=prepared.release_sha256,
        store=store,
        pointer_precondition=prepared.serving.precondition,
        before_pointer_switch=before_pointer_switch,
        verified_readback_root=verified_readback_root,
    )
    target_origin = LakeStoreOrigin(
        release_id=published.release_id,
        release_manifest_sha256=prepared.release_sha256,
    )
    try:
        advance_lake_store_origin(
            prepared.sqlite_path, expected=prepared.export.store_origin, target=target_origin
        )
    except LakeStoreOriginError as exc:
        raise LakePublishError(
            f"the lake pointer advanced but the local market store origin did not: {exc}; "
            "hydrate the store from current before publishing again"
        ) from exc
    print(
        "lake publish phases: "
        f"base_resolve={prepared.base_resolve_seconds:.3f}s "
        f"seal_plan_export={prepared.seal_export_seconds:.3f}s "
        f"release_create={prepared.release_create_seconds:.3f}s "
        f"local_graph={published.local_graph_seconds:.3f}s "
        f"remote_closure={published.remote_closure_seconds:.3f}s "
        f"pointer={published.pointer_seconds:.3f}s",
        file=sys.stderr,
    )
    transfers = published.transfers.as_dict()
    previous = prepared.serving.pointer
    return MarketLakePublishReport(
        release_id=published.release_id,
        release_manifest_sha256=prepared.release_sha256,
        data_as_of=prepared.release.data_as_of.isoformat(),
        previous_release_id=None if previous is None else previous.release_id,
        changed_partitions={
            name: item.new_objects for name, item in sorted(prepared.export.datasets.items())
        },
        uploaded_objects=int(transfers.get("uploaded_objects", 0)),
        uploaded_bytes=int(transfers.get("uploaded_bytes", 0)),
        transfers=published.transfers,
    )


def publish_market_lake(
    *,
    sqlite_path: Path,
    mirror_root: Path,
    store: ObjectStore,
    release_id: str | None = None,
    before_pointer_switch: Callable[[], None] | None = None,
    verified_readback_root: Path | None = None,
    edinet_input: InputSnapshot | None = None,
) -> MarketLakePublishReport:
    """単独公開の入口。共通統合はprepareとpublishの間でモデルを生成する。"""
    with prepare_market_lake(
        sqlite_path=sqlite_path,
        mirror_root=mirror_root,
        store=store,
        release_id=release_id,
        edinet_input=edinet_input,
    ) as prepared:
        return publish_prepared_market_lake(
            prepared,
            store=store,
            before_pointer_switch=before_pointer_switch,
            verified_readback_root=verified_readback_root,
        )


def _serving_pointer(store: ObjectStore) -> PointerSnapshot:
    return read_pointer_snapshot(store)


def _pointer_origin(pointer: LakeReleasePointer | None) -> LakeStoreOrigin | None:
    if pointer is None:
        return None
    return LakeStoreOrigin(
        release_id=pointer.release_id,
        release_manifest_sha256=pointer.manifest_sha256,
    )


def _remaining_source_pages_start(
    serving: LakeFixedRelease,
    store: ObjectStore,
    mirror_root: Path,
    migrated_sources: frozenset[str],
) -> date | None:
    """Keep the fixed old release's floor only for originals still owned by the lake."""
    dataset = LAKE_DATASETS["jquants.source_pages"]
    for partition in selected_partitions(serving, dataset.name):
        earliest = None
        for item in partition.objects:
            # Hydrate evicts large raw objects. Do not accumulate their second copies
            # beside this publication's seal and exports just to inspect two columns.
            with tempfile.TemporaryDirectory(
                dir=mirror_root, prefix=".source-history-"
            ) as directory:
                root = (
                    mirror_root
                    if lake_mirror_path(mirror_root, item.key).is_file()
                    else Path(directory)
                )
                path = _fetch(store, root, item.key, expected_sha256=item.sha256)
                verify_object(path, dataset, item)
                with pq.ParquetFile(path, pre_buffer=False) as parquet:
                    for batch in parquet.iter_batches(columns=["source", "received_on"]):
                        for row in batch.to_pylist():
                            if row["source"] not in migrated_sources:
                                received = date.fromisoformat(row["received_on"])
                                earliest = received if earliest is None else min(earliest, received)
        if earliest is not None:
            return earliest
    return None


@contextmanager
def _export_manifest_snapshot(
    datasets: Mapping[str, LakeBuildReport], mirror_root: Path
) -> Iterator[list[Path]]:
    """Freeze the exact manifest models checked by the export into private bytes."""

    mirror_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=mirror_root, prefix=".lake-publication-manifests-"
    ) as directory:
        root = Path(directory)
        paths: list[Path] = []
        for index, (name, item) in enumerate(sorted(datasets.items())):
            payload = canonical_lake_model_bytes(item.manifest)
            path = root / f"{index:02d}-{name.replace('.', '-')}.json"
            path.write_bytes(payload)
            paths.append(path)
        yield paths


def _resolve_serving_release(
    store: ObjectStore, mirror_root: Path, serving: LakeReleasePointer
) -> LakeFixedRelease:
    release_path = _fetch(
        store,
        mirror_root,
        serving.manifest_key,
        expected_sha256=serving.manifest_sha256,
    )
    try:
        release = load_lake_model_json(release_path.read_bytes(), LakeReleaseManifest)
    except ValueError:
        raise LakePublishError(
            f"serving release manifest is invalid: {serving.manifest_key}"
        ) from None
    for dataset_name, entry in sorted(release.datasets.items()):
        _fetch(
            store,
            mirror_root,
            lake_dataset_manifest_key(dataset=dataset_name, build_id=entry.build_id),
            expected_sha256=entry.manifest_sha256,
        )
    try:
        return resolve_release(
            LocalMirrorSource(mirror_root),
            serving.release_id,
            manifest_sha256=serving.manifest_sha256,
        )
    except LakeReadError as exc:
        raise LakePublishError(f"serving release identity validation failed: {exc}") from None


def _fetch(
    store: ObjectStore,
    mirror_root: Path,
    key: str,
    *,
    expected_sha256: str,
) -> Path:
    # Through the validating resolver rather than a join: these keys come off a remote
    # pointer, and a join would follow one that escaped the mirror.
    path = lake_mirror_path(mirror_root, key)
    if path.is_file() and sha256_file(path) == expected_sha256:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=f".{path.name}.") as directory:
        candidate = Path(directory) / path.name
        store.download_file(key, candidate)
        actual_sha256 = sha256_file(candidate)
        if actual_sha256 != expected_sha256:
            raise LakePublishError(
                f"downloaded lake object digest mismatch: {key}; "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
        candidate.replace(path)
        fsync_directory(path.parent)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--mirror", type=Path, required=True)
    parser.add_argument("--bucket", default="baibai-stores")
    parser.add_argument("--release-id")
    parser.add_argument("--lease-handle", type=Path, required=True)
    parser.add_argument("--saved-edinet-input", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        handle = _load_handle(args.lease_handle)
        store = Boto3R2Store(bucket=args.bucket)
        verify_held(store, handle)
        report = publish_market_lake(
            sqlite_path=args.sqlite,
            mirror_root=args.mirror,
            store=store,
            release_id=args.release_id,
            before_pointer_switch=lambda: verify_held(store, handle),
            edinet_input=(
                InputSnapshot.model_validate_json(args.saved_edinet_input.read_bytes())
                if args.saved_edinet_input is not None
                else None
            ),
        )
    except LakeBuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (LakePublishError, LeaseError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
