"""The calibration cohort store: immutable typed L2 builds under one bundle pointer.

A cohort update prepares panel, diagnostics, and forward dataset builds, then exposes
all three through one calibration bundle pointer. Objects are content addressed, so a
cohort that did not change costs nothing to carry into the next bundle, and a bundle
that was superseded remains addressable until retention decides otherwise.

Storage is typed Parquet whose schema is derived from ``PanelRow``,
``PanelDiagnostics``, and ``ForwardReturnRow``. Nothing here migrates a published
object: a contract change produces a new build, which is what keeps a cohort
measured under one set of rules from ever merging with a cohort measured under
another.

``CACHE_SCHEMA_VERSION`` remains the compatibility statement for the cohort, and it
is folded into the transform fingerprint of every build, so a store written under
different measurement rules is rejected rather than read.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from datetime import UTC, date, datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import cast

import yaml

from baibai_engine.foundation.filesystem import write_bytes_atomic, write_text_atomic
from baibai_engine.foundation.repository_layout import CALIBRATION_DIR
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market.lake.identity import verified_git_commit
from baibai_engine.market.lake.immutable import install_immutable_file
from baibai_engine.market.lake.keys import (
    calibration_bundle_manifest_key,
    current_calibration_bundle_pointer_key,
    dataset_manifest_key,
)
from baibai_engine.market.lake.models import (
    CalibrationInputManifest,
    CalibrationInputSourceRef,
    CohortInventoryEntry,
    CohortStatus,
    DatasetManifest,
    PartitionManifest,
    SourceRef,
    load_lake_model_json,
)
from baibai_engine.market.lake.objects import sha256_bytes, sha256_file
from baibai_engine.market.lake.retention import (
    LakeRetentionError,
    advance_l2_pointer,
    lake_writer_lock,
    read_l2_pointer,
)

from ..metrics import VALUATION_CALCULATION_REVISION
from ..rules import _RELAXED_THRESHOLDS as _RELAXED_TABLE
from .forward import (
    FORWARD_FIELD_NAMES,
    RESOLVED_STATUSES,
    TOTAL_RETURN_BASIS,
    TOTAL_RETURN_STATUSES,
    ForwardReturnRow,
)
from .lake import (
    CALIBRATION_DIAGNOSTICS,
    CALIBRATION_FORWARD,
    CALIBRATION_PANEL,
    L2_DATASETS,
    CalibrationBundleManifest,
    CalibrationBundlePointer,
    CalibrationBundleRef,
    CalibrationCohortInventory,
    CalibrationDatasetRef,
    CalibrationLakeError,
    FixedCalibrationBundle,
    L2BuildInputs,
    L2Dataset,
    _require_object,
    _write_immutable,
    asof_month,
    build_identifier,
    canonical_manifest_bytes,
    load_manifest,
    publish_l2_build,
    read_l2_partition,
    require_build_inputs,
    require_l2_dataset,
    require_manifest_contract,
    transform_fingerprint,
    write_l2_partition,
)
from .panel import (
    DIAGNOSTIC_FIELD_NAMES,
    PANEL_FIELD_NAMES,
    PanelDiagnostics,
    PanelRow,
    PopulationCoverageStatus,
)

RELAXED = _RELAXED_TABLE

DEFAULT_CALIBRATION_DIR = CALIBRATION_DIR

# The identity a build records when no L1 release was bound to it: every input came
# from the legacy store. It is a real, checkable value rather than an empty field, so
# a reader that requires a release fails on the value instead of on an absence.
LEGACY_ONLY_RELEASE_ID = "legacy-sqlite-only"


def _derive_cache_schema_version() -> str:
    """cohort が互換かどうかを、互換性を決める入力そのものから導く。

    手で進める版は、進める判断を人がするから忘れる。実際 2026-08 には列の形を変えずに
    観測の範囲だけを広げた変更で進め忘れ、独立レビューが見つけるまで新旧の cohort が
    1 つの集計へ混ざる状態だった。同じ列名で狭い観測と広い観測が並ぶと、測っていない
    ことが「効かなかった」として読まれる。

    互換性を決めるのは 3 つある。**列の形** (panel / diagnostics / forward の field)、
    **列に入る観測の範囲** (どの playbook 閾値をどの緩和値で測るか)、そして **列の値の
    意味** (`metrics.VALUATION_CALCULATION_REVISION`)。式の意味の変更だけは内容から
    導けないので人が宣言するが、宣言すれば cache 版もそれに従って動く。

    評価時にだけ読む軸の一覧 (`GATE_BASE_AXES` / `SECTOR_MEDIAN_AXES`) はここに入れない。
    どれも既存の panel 列を指すので、軸を足し引きしても cache の中身は 1 バイトも変わらず、
    版へ入れると 81 cohort・503MB の再構築を互換性上は不要な変更のたびに要求する。
    """
    contract = "|".join(
        (
            ",".join(PANEL_FIELD_NAMES),
            ",".join(DIAGNOSTIC_FIELD_NAMES),
            ",".join(FORWARD_FIELD_NAMES),
            # 閾値名だけでなく緩和値も入れる。同じ閾値を別の値で測った cohort は互換でない。
            ",".join(
                sorted(
                    "{}:{}".format(
                        name,
                        ",".join(f"{key}={value}" for key, value in sorted(thresholds.items())),
                    )
                    for name, thresholds in RELAXED.items()
                )
            ),
            VALUATION_CALCULATION_REVISION,
        )
    )
    return sha256(contract.encode("utf-8")).hexdigest()[:16]


CACHE_SCHEMA_VERSION = _derive_cache_schema_version()

_POPULATION_COVERAGE_STATUSES = {
    "evaluated",
    "priced_master_without_universe",
    "master_without_universe_unpriced",
}


class CalibrationCacheError(RuntimeError):
    """The local store cannot prove that it uses the current contract."""


def cache_meta_path(root: Path) -> Path:
    return root / "calibration.meta.yaml"


def _write_cache_meta(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        cache_meta_path(root),
        yaml.safe_dump({"cache_schema_version": CACHE_SCHEMA_VERSION}, sort_keys=False),
    )


def _require_current_cache(root: Path) -> None:
    path = cache_meta_path(root)
    if not path.exists():
        raise CalibrationCacheError(
            "calibration cache version is missing; run calibration-build --force"
        )
    try:
        payload = safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CalibrationCacheError(
            "calibration cache version is invalid; run calibration-build --force"
        ) from exc
    version = payload.get("cache_schema_version") if isinstance(payload, dict) else None
    if version != CACHE_SCHEMA_VERSION:
        raise CalibrationCacheError(
            "calibration cache version is incompatible; run calibration-build --force"
        )


def _inputs(
    root: Path,
    source: SourceRef | None = None,
    *,
    producer_commit: str | None = None,
) -> L2BuildInputs:
    """What this build declares, and nothing it does not have.

    No L1 release is bound while calibration's inputs are still read from the legacy
    store, so the recorded release is the named value that says so rather than an id
    that would imply a provenance the build did not have.
    """

    if source is None:
        input_id = LEGACY_ONLY_RELEASE_ID
        manifest = CalibrationInputManifest(
            manifest_version=1,
            input_id=input_id,
            input_type="local_operation",
            files={},
        )
        key = f"lake/manifests/calibration-inputs/{input_id}.json"
        path = root / key
        _write_immutable(path, canonical_manifest_bytes(manifest))
        source = CalibrationInputSourceRef(
            kind="calibration_input",
            source_id=input_id,
            key=key,
            sha256=sha256_file(path),
            input_type="local_operation",
            manifest_version=1,
        )
    return L2BuildInputs(
        sources=(source,),
        producer_git_commit=producer_commit or verified_git_commit(),
        cache_schema_version=CACHE_SCHEMA_VERSION,
    )


def _require_partition_objects(
    root: Path, dataset: L2Dataset, partition: PartitionManifest
) -> None:
    """A carried partition must still have its objects, or the build inherits a hole."""

    for item in partition.objects:
        if not (root / item.key).is_file():
            raise CalibrationLakeError(f"{dataset.name}: published object is missing: {item.key}")


def _writer_manifest(root: Path, dataset: L2Dataset) -> DatasetManifest | None:
    """The build the pointer names, or ``None`` when the dataset has no head yet.

    The pointer's digest is checked here. A manifest key is derived from
    ``(dataset, build_id)``, so without it the same pointer could be made to resolve
    to a different set of objects by replacing that key — and every check below it
    would pass, because the object digests it compares against would come from the
    replacement.
    """

    pointer = read_l2_pointer(root, dataset.name)
    if pointer is None:
        return None
    path = root / pointer.manifest_key
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise CalibrationLakeError(f"L2 dataset manifest is unreadable: {path}") from exc
    if sha256_bytes(payload) != pointer.manifest_sha256:
        raise CalibrationLakeError(f"{dataset.name}: manifest digest does not match the pointer")
    manifest = load_manifest(path)
    require_manifest_contract(dataset, manifest)
    return manifest


def _fixed_bundle(root: Path) -> FixedCalibrationBundle | None:
    """Resolve the public generation once and close every manifest edge by digest."""

    pointer_path = root / current_calibration_bundle_pointer_key()
    if not pointer_path.is_file():
        return None
    try:
        pointer = load_lake_model_json(pointer_path.read_bytes(), CalibrationBundlePointer)
        payload = (root / pointer.current.manifest_key).read_bytes()
    except (OSError, ValueError) as exc:
        raise CalibrationLakeError("calibration bundle pointer is unreadable") from exc
    if sha256_bytes(payload) != pointer.current.manifest_sha256:
        raise CalibrationLakeError("calibration bundle manifest digest differs from its pointer")
    try:
        bundle = load_lake_model_json(payload, CalibrationBundleManifest)
    except ValueError as exc:
        raise CalibrationLakeError("calibration bundle manifest is invalid") from exc
    if bundle.bundle_id != pointer.current.bundle_id:
        raise CalibrationLakeError("calibration bundle identity differs from its pointer")
    manifests: dict[str, DatasetManifest] = {}
    for name, reference in bundle.datasets.items():
        try:
            manifest_payload = (root / reference.manifest_key).read_bytes()
        except OSError as exc:
            raise CalibrationLakeError(f"calibration bundle dataset is unreadable: {name}") from exc
        if sha256_bytes(manifest_payload) != reference.manifest_sha256:
            raise CalibrationLakeError(f"calibration bundle dataset digest differs: {name}")
        manifest = load_manifest(root / reference.manifest_key)
        require_manifest_contract(require_l2_dataset(name), manifest)
        if manifest.build_id != reference.build_id or manifest.totals.rows != reference.rows:
            raise CalibrationLakeError(f"calibration bundle dataset identity differs: {name}")
        manifests[name] = manifest
    for asof, cohort in bundle.cohorts.items():
        expected = {
            CALIBRATION_PANEL.name: cohort.panel,
            CALIBRATION_DIAGNOSTICS.name: cohort.diagnostics,
            CALIBRATION_FORWARD.name: cohort.forward,
        }
        for name, entry in expected.items():
            if manifests[name].cohort_inventory.get(asof) != entry:
                raise CalibrationLakeError(f"calibration bundle cohort inventory differs: {asof}")
    return FixedCalibrationBundle(
        ref=pointer.current,
        manifest=bundle,
        datasets=MappingProxyType(manifests),
    )


def _current_manifest(root: Path, dataset: L2Dataset) -> DatasetManifest | None:
    bundle = _fixed_bundle(root)
    return None if bundle is None else bundle.datasets[dataset.name]


def _publish_bundle(root: Path) -> CalibrationBundleRef:
    manifests: dict[str, DatasetManifest] = {}
    references: dict[str, CalibrationDatasetRef] = {}
    for name, dataset in L2_DATASETS.items():
        manifest = _writer_manifest(root, dataset)
        if manifest is None:
            raise CalibrationLakeError(f"bundle dataset has no published build: {name}")
        path = root / dataset_manifest_key(dataset=name, build_id=manifest.build_id)
        digest = sha256_bytes(path.read_bytes())
        manifests[name] = manifest
        require_build_inputs(
            manifest,
            dataset=dataset,
            cache_schema_version=CACHE_SCHEMA_VERSION,
        )
        references[name] = CalibrationDatasetRef(
            dataset=name,
            build_id=manifest.build_id,
            manifest_key=path.relative_to(root).as_posix(),
            manifest_sha256=digest,
            rows=manifest.totals.rows,
        )
    cohort_keys = set(manifests[CALIBRATION_PANEL.name].cohort_inventory)
    if set(manifests[CALIBRATION_DIAGNOSTICS.name].cohort_inventory) != cohort_keys:
        raise CalibrationLakeError("panel and diagnostics cohort inventory differ")
    if set(manifests[CALIBRATION_FORWARD.name].cohort_inventory) != cohort_keys:
        raise CalibrationLakeError("panel and forward cohort inventory differ")
    source_sets = {
        tuple((item.kind, item.source_id, item.key, item.sha256) for item in manifest.sources)
        for manifest in manifests.values()
    }
    if len(source_sets) != 1:
        raise CalibrationLakeError("calibration bundle datasets have different input generations")
    producer_commits = {manifest.producer_git_commit for manifest in manifests.values()}
    if len(producer_commits) != 1:
        raise CalibrationLakeError("calibration bundle datasets have different producers")
    cohorts = {
        asof: CalibrationCohortInventory(
            panel=manifests[CALIBRATION_PANEL.name].cohort_inventory[asof],
            diagnostics=manifests[CALIBRATION_DIAGNOSTICS.name].cohort_inventory[asof],
            forward=manifests[CALIBRATION_FORWARD.name].cohort_inventory[asof],
        )
        for asof in sorted(cohort_keys)
    }
    now = datetime.now(UTC)
    bundle_id = f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}"
    bundle_manifest = CalibrationBundleManifest(
        bundle_id=bundle_id,
        created_at=now,
        producer_git_commit=producer_commits.pop(),
        cache_schema_version=CACHE_SCHEMA_VERSION,
        datasets=references,
        cohorts=cohorts,
    )
    key = calibration_bundle_manifest_key(bundle_id=bundle_id)
    path = root / key
    _write_immutable(path, canonical_manifest_bytes(bundle_manifest))
    reference = CalibrationBundleRef(
        bundle_id=bundle_id,
        manifest_key=key,
        manifest_sha256=sha256_bytes(path.read_bytes()),
    )
    pointer_path = root / current_calibration_bundle_pointer_key()
    previous = None
    if pointer_path.is_file():
        previous = load_lake_model_json(pointer_path.read_bytes(), CalibrationBundlePointer).current
    write_bytes_atomic(
        pointer_path,
        canonical_manifest_bytes(CalibrationBundlePointer(current=reference, previous=previous)),
    )
    return reference


def current_bundle_ref(root: Path) -> CalibrationBundleRef | None:
    fixed = _fixed_bundle(root)
    return None if fixed is None else fixed.ref


def resolve_calibration_bundle(root: Path) -> FixedCalibrationBundle:
    fixed = _fixed_bundle(root)
    if fixed is None:
        raise CalibrationCacheError("calibration bundle is absent")
    return fixed


def adopt_bundle_generation(
    root: Path,
    generated_root: Path,
    *,
    expected_current: CalibrationBundleRef | None,
) -> CalibrationBundleRef:
    """Install a verified force build and atomically expose only its bundle pointer."""

    fixed = _fixed_bundle(generated_root)
    if fixed is None:
        raise CalibrationLakeError("generated calibration bundle is missing")
    for name, manifest in fixed.datasets.items():
        dataset = require_l2_dataset(name)
        for partition in manifest.partitions:
            for item in partition.objects:
                _require_object(generated_root / item.key, dataset=dataset, item=item)

    pointer_path = root / current_calibration_bundle_pointer_key()
    actual = current_bundle_ref(root)
    if actual != expected_current:
        raise CalibrationLakeError("calibration bundle moved while force build was in flight")

    for source in sorted((generated_root / "lake").rglob("*")):
        if not source.is_file():
            continue
        key = source.relative_to(generated_root).as_posix()
        if key.startswith(("lake/pointers/", "lake/staging/", "lake/audit/", "lake/retention/")):
            continue
        target = root / key
        install_immutable_file(target, source, expected_sha256=sha256_file(source))

    _write_cache_meta(root)
    write_bytes_atomic(
        pointer_path,
        canonical_manifest_bytes(CalibrationBundlePointer(current=fixed.ref, previous=actual)),
    )
    return fixed.ref


def _publish_cohort(
    root: Path,
    *,
    dataset: L2Dataset,
    asof: date,
    rows: Sequence[object],
    status: CohortStatus | None = None,
    source: SourceRef | None = None,
    producer_commit: str | None = None,
) -> None:
    """Publish a build that carries every cohort already published plus this one.

    A month can hold more than one cohort, so the partition being replaced is
    rebuilt from the rows the current build holds for the other as-ofs plus the new
    ones. Content addressing makes the untouched months resolve to the objects that
    are already stored.
    """

    root.mkdir(parents=True, exist_ok=True)
    month = (asof.year, asof.month)
    manifest = _current_manifest(root, dataset)
    writer_pointer = read_l2_pointer(root, dataset.name)
    if manifest is not None:
        # Carrying a partition from a build made under other measurement rules would
        # publish it under this build's fingerprint, which is exactly the mixing the
        # cohort contract exists to prevent. The version stamp is written only after
        # this passes, so a refused build leaves the store describing itself truthfully.
        require_build_inputs(manifest, dataset=dataset, cache_schema_version=CACHE_SCHEMA_VERSION)
    _write_cache_meta(root)
    inputs = _inputs(root, source, producer_commit=producer_commit)
    if manifest is not None:
        by_identity = {
            (item.kind, item.source_id, item.key, item.sha256): item
            for item in (*manifest.sources, *inputs.sources)
        }
        inputs = L2BuildInputs(
            sources=tuple(by_identity[key] for key in sorted(by_identity)),
            producer_git_commit=inputs.producer_git_commit,
            cache_schema_version=inputs.cache_schema_version,
        )

    carried: list[PartitionManifest] = []
    inventory = {} if manifest is None else dict(manifest.cohort_inventory)
    same_month: list[object] = []
    data_as_of = asof
    if manifest is not None:
        data_as_of = max(data_as_of, manifest.data_as_of)
        for partition in manifest.partitions:
            other = (int(partition.values["year"]), int(partition.values["month"]))
            if other != month:
                _require_partition_objects(root, dataset, partition)
                carried.append(partition)
                continue
            for payload in read_l2_partition(
                dataset=dataset, manifest=manifest, mirror_root=root, month=other
            ):
                if str(payload["asof"]) != asof.isoformat():
                    same_month.append(_materialize(dataset, payload))
    replacement = write_l2_partition(
        dataset=dataset,
        mirror_root=root,
        month=month,
        rows=[*same_month, *rows],
        inputs=inputs,
    )
    if replacement is not None:
        carried.append(replacement)
    cohort_status: CohortStatus = status or ("complete" if rows else "empty")
    inventory[asof.isoformat()] = CohortInventoryEntry(status=cohort_status, rows=len(rows))

    fingerprint = transform_fingerprint(dataset, cache_schema_version=CACHE_SCHEMA_VERSION)
    now = datetime.now(UTC)
    report = publish_l2_build(
        dataset=dataset,
        mirror_root=root,
        partitions=carried,
        inputs=inputs,
        build_id=build_identifier(dataset=dataset, fingerprint=fingerprint, now=now),
        data_as_of=data_as_of,
        cohort_inventory=inventory,
        created_at=now,
    )
    advance_l2_pointer(
        root,
        dataset=dataset.name,
        build_id=report.build_id,
        manifest_path=report.manifest_path,
        expected_current_build_id=(None if writer_pointer is None else writer_pointer.build_id),
    )


def _materialize(dataset: L2Dataset, payload: Mapping[str, object]) -> object:
    if dataset.name == CALIBRATION_PANEL.name:
        return panel_row_from_mapping(payload)
    if dataset.name == CALIBRATION_DIAGNOSTICS.name:
        return PanelDiagnostics(**payload)  # type: ignore[arg-type]
    return forward_row_from_mapping(payload)


def write_panel(
    root: Path,
    asof: date,
    rows: tuple[PanelRow, ...],
    diagnostics: PanelDiagnostics,
    *,
    source: SourceRef | None = None,
    producer_commit: str | None = None,
    lock_held: bool = False,
) -> None:
    publication = nullcontext() if lock_held else lake_writer_lock(root)
    with _store_errors(), publication:
        _publish_cohort(
            root,
            dataset=CALIBRATION_PANEL,
            asof=asof,
            rows=rows,
            source=source,
            producer_commit=producer_commit,
        )
        _publish_cohort(
            root,
            dataset=CALIBRATION_DIAGNOSTICS,
            asof=asof,
            rows=(diagnostics,),
            source=source,
            producer_commit=producer_commit,
        )
        forward = _writer_manifest(root, CALIBRATION_FORWARD)
        if forward is None or asof.isoformat() not in forward.cohort_inventory:
            _publish_cohort(
                root,
                dataset=CALIBRATION_FORWARD,
                asof=asof,
                rows=(),
                status="not_computed",
                source=source,
                producer_commit=producer_commit,
            )
        _publish_bundle(root)


def write_forward(
    root: Path,
    asof: date,
    rows: list[ForwardReturnRow],
    *,
    source: SourceRef | None = None,
    producer_commit: str | None = None,
    lock_held: bool = False,
) -> None:
    publication = nullcontext() if lock_held else lake_writer_lock(root)
    with _store_errors(), publication:
        _publish_cohort(
            root,
            dataset=CALIBRATION_FORWARD,
            asof=asof,
            rows=rows,
            source=source,
            producer_commit=producer_commit,
        )
        _publish_bundle(root)


@contextmanager
def _store_errors() -> Iterator[None]:
    """Present one error type at the store boundary, on the write side too.

    The read side already translates, and a caller that handles a failed read but
    receives a raw lower-layer exception from a failed write ends up as a traceback
    instead of the message that tells the operator to rebuild.
    """

    try:
        yield
    except (CalibrationLakeError, LakeRetentionError) as exc:
        raise CalibrationCacheError(str(exc)) from exc


def has_cohort(root: Path, asof: date) -> bool:
    """Whether the current panel build already holds this cohort.

    Only "there is no build yet" answers False. A store that exists but cannot be
    read — a drifted contract, a missing object, an unreadable pointer — raises, so a
    build never treats a broken store as an empty one and rebuilds into it.
    """

    try:
        bundle = _fixed_bundle(root)
        if bundle is None:
            return False
        entry = bundle.manifest.cohorts.get(asof.isoformat())
        if entry is None or entry.panel.status not in {"complete", "empty"}:
            return False
        if entry.panel.status == "empty":
            return True
        return bool(_cohort_payloads(root, CALIBRATION_PANEL, asof))
    except (CalibrationLakeError, LakeRetentionError) as exc:
        raise CalibrationCacheError(str(exc)) from exc


def published_cohorts(root: Path, *, bundle: FixedCalibrationBundle | None = None) -> list[date]:
    """Every as-of the current panel build holds, in order.

    The build manifest is the inventory: a cohort exists because the current build
    publishes rows for it, not because a file with a matching name is on disk.
    """

    try:
        fixed = bundle or _fixed_bundle(root)
        if fixed is None:
            return []
        for partition in fixed.datasets[CALIBRATION_PANEL.name].partitions:
            _require_partition_objects(root, CALIBRATION_PANEL, partition)
        asofs = {
            date.fromisoformat(asof)
            for asof, entry in fixed.manifest.cohorts.items()
            if entry.panel.status in {"complete", "empty"}
        }
    except (CalibrationLakeError, LakeRetentionError) as exc:
        # An unreadable build is not an empty store. Reporting it as "no cohorts"
        # would make evaluate say the store holds nothing when it holds 81 cohorts.
        raise CalibrationCacheError(f"calibration cache is invalid: {exc}") from exc
    return sorted(asofs)


def _cohort_payloads(
    root: Path,
    dataset: L2Dataset,
    asof: date,
    *,
    bundle: FixedCalibrationBundle | None = None,
) -> list[Mapping[str, object]]:
    fixed = bundle or _fixed_bundle(root)
    if fixed is None:
        return []
    manifest = fixed.datasets[dataset.name]
    cohort = fixed.manifest.cohorts.get(asof.isoformat())
    if cohort is None:
        return []
    entry = {
        CALIBRATION_PANEL.name: cohort.panel,
        CALIBRATION_DIAGNOSTICS.name: cohort.diagnostics,
        CALIBRATION_FORWARD.name: cohort.forward,
    }[dataset.name]
    if entry.status in {"partial", "not_computed"}:
        raise CalibrationLakeError(f"{dataset.name}: cohort is {entry.status}")
    require_build_inputs(manifest, dataset=dataset, cache_schema_version=CACHE_SCHEMA_VERSION)
    payloads = read_l2_partition(
        dataset=dataset, manifest=manifest, mirror_root=root, month=asof_month(asof.isoformat())
    )
    selected: list[Mapping[str, object]] = [
        item for item in payloads if str(item["asof"]) == asof.isoformat()
    ]
    if len(selected) != entry.rows:
        raise CalibrationLakeError(f"{dataset.name}: cohort row count differs from inventory")
    return selected


def read_panel(
    root: Path, asof: date, *, bundle: FixedCalibrationBundle | None = None
) -> list[PanelRow]:
    _require_current_cache(root)
    try:
        fixed = bundle or _fixed_bundle(root)
        payloads = _cohort_payloads(root, CALIBRATION_PANEL, asof, bundle=fixed)
    except (CalibrationLakeError, LakeRetentionError) as exc:
        raise CalibrationCacheError(f"calibration panel cache is invalid: {exc}") from exc
    entry = None if fixed is None else fixed.manifest.cohorts.get(asof.isoformat())
    if entry is not None and entry.panel.status == "empty":
        return []
    if not payloads:
        raise CalibrationCacheError("calibration cache is partial; run calibration-build --force")
    try:
        return [panel_row_from_mapping(payload) for payload in payloads]
    except (KeyError, TypeError, ValueError) as exc:
        raise CalibrationCacheError(
            "calibration panel cache is invalid; run calibration-build --force"
        ) from exc


def read_panel_meta(
    root: Path, asof: date, *, bundle: FixedCalibrationBundle | None = None
) -> dict[str, object]:
    _require_current_cache(root)
    try:
        payloads = _cohort_payloads(root, CALIBRATION_DIAGNOSTICS, asof, bundle=bundle)
    except (CalibrationLakeError, LakeRetentionError) as exc:
        raise CalibrationCacheError(f"calibration cache metadata is invalid: {exc}") from exc
    if not payloads:
        raise CalibrationCacheError("calibration cache is partial; run calibration-build --force")
    if len(payloads) != 1:
        raise CalibrationCacheError(
            "calibration cache holds more than one diagnostics row for a cohort"
        )
    payload = dict(payloads[0])
    if not isinstance(payload.get("rules_hash"), str):
        raise CalibrationCacheError(
            "calibration cache metadata is invalid; run calibration-build --force"
        )
    return payload


def read_forward(
    root: Path, asof: date, *, bundle: FixedCalibrationBundle | None = None
) -> list[ForwardReturnRow]:
    _require_current_cache(root)
    try:
        fixed = bundle or _fixed_bundle(root)
        if fixed is None:
            raise CalibrationCacheError(
                "calibration cache is partial; run calibration-build --force"
            )
        manifest = fixed.datasets[CALIBRATION_FORWARD.name]
        cohort = fixed.manifest.cohorts.get(asof.isoformat())
        if cohort is None or cohort.forward.status in {"partial", "not_computed"}:
            raise CalibrationCacheError(
                "calibration cache is partial; run calibration-build --force"
            )
        require_build_inputs(
            manifest, dataset=CALIBRATION_FORWARD, cache_schema_version=CACHE_SCHEMA_VERSION
        )
        payloads = [
            item
            for item in read_l2_partition(
                dataset=CALIBRATION_FORWARD,
                manifest=manifest,
                mirror_root=root,
                month=asof_month(asof.isoformat()),
            )
            if str(item["asof"]) == asof.isoformat()
        ]
        if len(payloads) != cohort.forward.rows:
            raise CalibrationLakeError("calibration.forward: cohort row count differs")
    except (CalibrationLakeError, LakeRetentionError) as exc:
        raise CalibrationCacheError(f"calibration forward cache is invalid: {exc}") from exc
    try:
        return [forward_row_from_mapping(payload) for payload in payloads]
    except (KeyError, TypeError, ValueError) as exc:
        raise CalibrationCacheError(
            "calibration forward cache is invalid; run calibration-build --force"
        ) from exc


def panel_row_from_mapping(raw: Mapping[str, object]) -> PanelRow:
    """Build one panel row from stored values, checking the invariants it must hold."""

    row = PanelRow(**cast(dict[str, object], dict(raw)))  # type: ignore[arg-type]
    _population_coverage_status(str(row.population_coverage_status))
    _validate_asset_backed(row)
    _validate_shareholder_return_change(row)
    _validate_margin_supply_demand(row)
    _validate_normalized_profit(row)
    _validate_profitability_levels(row)
    return row


def forward_row_from_mapping(raw: Mapping[str, object]) -> ForwardReturnRow:
    row = ForwardReturnRow(**cast(dict[str, object], dict(raw)))  # type: ignore[arg-type]
    _validate_total_return_contract(row)
    return row


def _validate_total_return_contract(row: ForwardReturnRow) -> None:
    if row.resolved is not (row.status in RESOLVED_STATUSES):
        raise ValueError(f"resolved flag disagrees with status: {row.status!r}")
    if row.total_return_basis != TOTAL_RETURN_BASIS:
        raise ValueError(f"invalid total return basis: {row.total_return_basis!r}")
    if row.total_return_status not in TOTAL_RETURN_STATUSES:
        raise ValueError(f"invalid total return status: {row.total_return_status!r}")
    if row.total_return_status == "resolved":
        values = (row.price_return, row.realized_dividend_sum, row.total_return)
        if (
            not row.resolved
            or row.realized_dividend_fy_count <= 0
            or any(value is None or not isfinite(value) for value in values)
            or (row.realized_dividend_sum or 0.0) < 0
            or (row.total_return or 0.0) < -1
            or (row.total_return or 0.0) < (row.price_return or 0.0)
        ):
            raise ValueError("resolved total return fields are inconsistent")
    elif (
        row.realized_dividend_sum is not None
        or row.realized_dividend_fy_count != 0
        or row.total_return is not None
    ):
        raise ValueError("unresolved total return carries resolved values")


def _validate_asset_backed(row: PanelRow) -> None:
    investment = row.investment_securities
    if investment is not None and (not isfinite(investment) or investment < 0):
        raise ValueError("investment securities must be finite and non-negative")
    ratio = row.asset_backed_ratio
    if ratio is None:
        return
    if not isfinite(ratio):
        raise ValueError("asset-backed ratio must be finite")
    if investment is None or row.net_cash_to_market_cap is None:
        raise ValueError("asset-backed ratio requires its source fields")
    if row.market_cap_oku is None:
        if row.in_population:
            raise ValueError("population asset-backed ratio requires market cap")
        if ratio < row.net_cash_to_market_cap - 1e-12:
            raise ValueError("asset-backed ratio is inconsistent with non-negative investment")
        return
    if row.market_cap_oku <= 0:
        raise ValueError("asset-backed ratio requires positive market cap")
    # market_cap_oku is the liquidity snapshot rounded to whole oku, while both
    # ratios use the exact close * shares market cap. Validate the investment
    # component against the exact-value interval represented by that rounded fact.
    component = ratio - row.net_cash_to_market_cap
    lower_market_cap = max((row.market_cap_oku - 0.5) * 100_000_000, 1.0)
    upper_market_cap = (row.market_cap_oku + 0.5) * 100_000_000
    component_min = investment / upper_market_cap
    component_max = investment / lower_market_cap
    tolerance = 1e-12 * max(1.0, abs(component), abs(component_max))
    if component < component_min - tolerance or component > component_max + tolerance:
        raise ValueError("asset-backed ratio is inconsistent with its source fields")


def _validate_shareholder_return_change(row: PanelRow) -> None:
    if row.dps_yoy_latest is not None and not isfinite(row.dps_yoy_latest):
        raise ValueError("DPS YoY must be finite")
    streak = row.share_count_reduction_streak
    if streak is not None and streak not in {0, 1, 2}:
        raise ValueError("share count reduction streak must be between zero and two")

    observed_positive = (
        (row.dps_yoy_latest is not None and row.dps_yoy_latest > 0)
        or row.dps_guidance_up is True
        or row.dividend_initiation is True
        or (streak is not None and streak >= 1)
    )
    all_observed_negative = (
        row.dps_yoy_latest is not None
        and row.dps_yoy_latest <= 0
        and row.dps_guidance_up is False
        and row.dividend_initiation is False
        and streak == 0
    )
    expected = True if observed_positive else False if all_observed_negative else None
    if row.shareholder_return_change is not expected:
        raise ValueError("shareholder return change is inconsistent with its components")


def _validate_margin_supply_demand(row: PanelRow) -> None:
    short_to_adv = row.margin_short_to_adv
    if short_to_adv is not None and (
        not isfinite(short_to_adv) or short_to_adv < 0 or row.margin_week_end is None
    ):
        raise ValueError("margin short to ADV requires a dated non-negative value")

    volatility = row.realized_volatility_60d
    if volatility is not None and (not isfinite(volatility) or volatility < 0):
        raise ValueError("realized volatility must be finite and non-negative")


def _validate_normalized_profit(row: PanelRow) -> None:
    for value in (row.normalized_per_3fy, row.normalized_per_5fy):
        if value is not None and (not isfinite(value) or value <= 0):
            raise ValueError("normalized PER must be finite and positive")
    if row.self_range_observed_sessions < 0:
        raise ValueError("self-range observed sessions must be non-negative")


def _validate_profitability_levels(row: PanelRow) -> None:
    levels = (
        row.operating_profit_to_assets,
        row.operating_margin,
        row.asset_turnover,
    )
    if any(value is not None and not isfinite(value) for value in levels):
        raise ValueError("profitability levels must be finite")
    if all(value is not None for value in levels):
        assert row.operating_margin is not None
        assert row.asset_turnover is not None
        assert row.operating_profit_to_assets is not None
        expected = row.operating_margin * row.asset_turnover
        if not abs(row.operating_profit_to_assets - expected) <= 1e-12 * max(1.0, abs(expected)):
            raise ValueError("profitability levels violate the accounting identity")


def _population_coverage_status(value: str) -> PopulationCoverageStatus:
    if value not in _POPULATION_COVERAGE_STATUSES:
        raise ValueError(f"invalid population coverage status: {value!r}")
    return cast(PopulationCoverageStatus, value)
