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

Compatibility is stated per dataset and nowhere else. ``CACHE_SCHEMA_VERSIONS`` is
folded into the transform fingerprint of each build, so a build written under different
measurement rules is rejected rather than read, and a contract change to one dataset
leaves the other two readable. ``CACHE_SCHEMA_VERSION`` is a store-wide summary of the
three used to decide whether a *legacy CSV* store is migratable; no published generation
records it, because a summary nothing derives or verifies is a second statement of a
fact the dataset manifests already carry.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import cast

from baibai_engine.foundation.filesystem import write_bytes_atomic
from baibai_engine.foundation.repository_layout import CALIBRATION_DIR
from baibai_engine.market.lake.identity import verified_git_commit
from baibai_engine.market.lake.immutable import install_immutable_file
from baibai_engine.market.lake.keys import (
    calibration_bundle_manifest_key,
    current_calibration_bundle_pointer_key,
    dataset_manifest_key,
)
from baibai_engine.market.lake.models import (
    CalibrationInputManifest,
    CohortInventoryEntry,
    CohortSourceRef,
    CohortStatus,
    DatasetManifest,
    ForwardObservationPolicyRef,
    MeasurementPolicyRef,
    PartitionManifest,
    load_lake_model_json,
    retained_sources,
)
from baibai_engine.market.lake.objects import sha256_bytes, sha256_file
from baibai_engine.market.lake.retention import LakeRetentionError, lake_writer_lock
from baibai_engine.market.lake.sources import resolve_source_ref

from ..metrics import VALUATION_CALCULATION_REVISION
from ..rules import _RELAXED_THRESHOLDS as _RELAXED_TABLE
from .forward import (
    DEFAULT_FORWARD_OBSERVATION_POLICY,
    FORWARD_FIELD_NAMES,
    RESOLVED_STATUSES,
    TOTAL_RETURN_BASIS,
    TOTAL_RETURN_STATUSES,
    ForwardObservationPolicy,
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
    require_manifest_layout,
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


def _derive_cache_schema_version() -> dict[str, str]:
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

    版は dataset ごとに導く。forward へ列を 1 つ足したときに panel の 81 cohort まで
    再構築になるのは、無関係な変更で数時間の運用と数百 MB を使うということで、しかも
    「再構築が要る」という信号の意味を薄める。screening 側の閾値と評価式の意味は panel と
    diagnostics の値を決めるが、forward の観測 (entry / exit / 配当) は決めない。
    """
    measurement = "|".join(
        (
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
    return {
        CALIBRATION_PANEL.name: _digest(",".join(PANEL_FIELD_NAMES), measurement),
        CALIBRATION_DIAGNOSTICS.name: _digest(",".join(DIAGNOSTIC_FIELD_NAMES), measurement),
        CALIBRATION_FORWARD.name: _digest(",".join(FORWARD_FIELD_NAMES)),
    }


def _digest(*parts: str) -> str:
    return sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


CACHE_SCHEMA_VERSIONS = _derive_cache_schema_version()
# The store-level statement: one value that moves when any dataset's contract moves, so a
# store can still say in one field which contract it was written under.
CACHE_SCHEMA_VERSION = _digest(
    *(CACHE_SCHEMA_VERSIONS[name] for name in sorted(CACHE_SCHEMA_VERSIONS))
)

_POPULATION_COVERAGE_STATUSES = {
    "evaluated",
    "priced_master_without_universe",
    "master_without_universe_unpriced",
}


class CalibrationCacheError(RuntimeError):
    """The local store cannot prove that it uses the current contract."""


def _published_generation(root: Path) -> FixedCalibrationBundle:
    """The generation the pointer serves, resolved once with every edge closed."""

    try:
        bundle = _fixed_bundle(root)
    except (CalibrationLakeError, LakeRetentionError) as exc:
        raise CalibrationCacheError(str(exc)) from exc
    if bundle is None:
        raise CalibrationCacheError("calibration cache is missing; run calibration-build --force")
    return bundle


def _read_generation(root: Path, bundle: FixedCalibrationBundle | None) -> FixedCalibrationBundle:
    """The generation a read acts on: the one it was handed, or the one served.

    A caller that already fixed a generation must not have the current pointer consulted
    again on its behalf, for anything — not for rows, and not for the observation policy
    the rows are validated against. Re-reading it makes the run's inputs depend on when
    it ran relative to somebody else's publication: the same fixed bundle would read on
    one attempt and fail on the next because current moved, and a run started against a
    pinned generation would fail if current were deleted underneath it.
    """

    return bundle if bundle is not None else _published_generation(root)


def _published_policy(root: Path) -> ForwardObservationPolicy:
    """Fix the served generation and return the observation rules it states.

    The contract is read from the bundle manifest the pointer names, so the answer can
    never describe a generation other than the one being served. The stated policy only
    selects which forward identity to expect: the build's own fingerprint is what proves
    the rows were produced under it, so a rewritten statement can cause a refusal but
    never an acceptance.

    Compatibility is not decided here. It is a per-dataset question — that is the whole
    point of deriving a contract version per dataset — and each read already asks it of
    the dataset it is about to read, through the transform fingerprint on that dataset's
    manifest. Asking the bundle-wide question first would undo the split: a change to the
    forward contract alone moves the aggregate, and every panel read in the store would
    refuse until all 81 panel cohorts were rebuilt for a contract that did not move.
    """

    return _policy_of(_published_generation(root))


def _policy_of(bundle: FixedCalibrationBundle) -> ForwardObservationPolicy:
    return ForwardObservationPolicy(
        use_control_event_exits=bundle.manifest.forward_observation_policy.use_control_event_exits
    )


def _require_current_contract(root: Path) -> None:
    """Refuse a whole generation this build cannot serve, dataset by dataset.

    Adoption is the one act that has to ask the bundle-wide question: it makes a
    generation canonical for every reader, so all three datasets must be ones this build
    can produce and read. It asks it as three per-dataset questions rather than as one
    aggregate, so the answer names the dataset that has to be rebuilt — and it asks both
    halves, contract version and transform, because resolution asks neither.
    """

    bundle = _published_generation(root)
    policy = _policy_of(bundle)
    for name, manifest in bundle.datasets.items():
        dataset = require_l2_dataset(name)
        require_manifest_contract(dataset, manifest)
        require_build_inputs(
            manifest,
            dataset=dataset,
            cache_schema_version=CACHE_SCHEMA_VERSIONS[name],
            forward_policy=policy,
        )


def store_forward_policy(root: Path) -> ForwardObservationPolicy:
    """The observation rules this store's forward builds must be identified by."""
    return _published_policy(root)


def _inputs(
    root: Path,
    dataset: L2Dataset,
    source: CohortSourceRef,
    *,
    producer_commit: str | None = None,
    forward_policy: ForwardObservationPolicy = DEFAULT_FORWARD_OBSERVATION_POLICY,
) -> L2BuildInputs:
    """Resolve and fix the exact source generation used by one cohort write."""

    for retained in retained_sources((source,)):
        resolve_source_ref(root, retained)
    return L2BuildInputs(
        sources=(source,),
        producer_git_commit=producer_commit or verified_git_commit(),
        cache_schema_version=CACHE_SCHEMA_VERSIONS[dataset.name],
        forward_policy=forward_policy,
    )


def _require_partition_objects(
    root: Path, dataset: L2Dataset, partition: PartitionManifest
) -> None:
    """A carried partition must still have its objects at their published size.

    Presence and size are what every cohort write can afford to check: a build touches
    one month and carries the rest, so hashing the whole dataset here would cost the
    dataset's full size once per cohort. The complete digest, schema, and row-count
    check runs once over the whole closure in ``adopt_bundle_generation``, which is the
    only path that makes a generation the canonical current one.
    """

    for item in partition.objects:
        path = root / item.key
        if not path.is_file():
            raise CalibrationLakeError(f"{dataset.name}: published object is missing: {item.key}")
        if path.stat().st_size != item.bytes:
            raise CalibrationLakeError(f"{dataset.name}: published object differs: {item.key}")


def _pointer_ref(root: Path) -> CalibrationBundleRef | None:
    """The reference the pointer names, without resolving the generation behind it.

    A compare-and-set asks whether the pointer moved, which is a question about the
    pointer. Resolving the generation to answer it makes replacing an unreadable one
    impossible: the repair that exists to install a working pointer would have to
    resolve the broken one first. What the closure of the incoming generation must
    prove is proved separately, and against the generation being installed.
    """

    pointer_path = root / current_calibration_bundle_pointer_key()
    if not pointer_path.is_file():
        return None
    try:
        return load_lake_model_json(pointer_path.read_bytes(), CalibrationBundlePointer).current
    except ValueError as exc:
        raise CalibrationLakeError("calibration bundle pointer is unreadable") from exc


def _bundle_manifest_dir(root: Path) -> Path:
    """Where published bundle manifests live, taken from the canonical key itself."""
    return (root / calibration_bundle_manifest_key(bundle_id="probe")).parent


def _has_published_generation(root: Path) -> bool:
    """Whether this store ever published a generation, its pointer aside.

    An absent pointer means "nothing published yet" only when nothing was published.
    Recovery is what makes the difference matter: moving an unreadable pointer aside
    turns a broken store into one that looks new, and the next ordinary build then has
    no inventory to be measured against and republishes a fraction of the history as the
    whole of it. Retention already refuses to sweep on exactly this distinction; a store
    that answers "empty" to the reader while answering "broken" to the collector is the
    disagreement, not the rule.

    Bundle manifests are the evidence rather than dataset manifests, because a
    generation writes its dataset manifests before the bundle that names them and this
    is read in between: a store mid-publication has not lost anything.
    """

    return any(_bundle_manifest_dir(root).glob("*.json"))


def _fixed_bundle(root: Path) -> FixedCalibrationBundle | None:
    """Resolve the public generation once and close every manifest edge by digest."""

    pointer_path = root / current_calibration_bundle_pointer_key()
    if not pointer_path.is_file():
        if _has_published_generation(root):
            raise CalibrationLakeError(
                "calibration bundle pointer is missing from a store that has already "
                "published generations"
            )
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
        require_manifest_layout(require_l2_dataset(name), manifest)
        if manifest.build_id != reference.build_id or manifest.totals.rows != reference.rows:
            raise CalibrationLakeError(f"calibration bundle dataset identity differs: {name}")
        manifests[name] = manifest
    # Both directions. Checking only that each cohort the bundle lists is present in the
    # manifests would accept a dataset holding cohorts the bundle does not publish, so
    # the same build would mean one set of as-ofs on its surface and another inside, and
    # the extra objects would be reachable through the bundle while described by nothing
    # in it. The assembler cannot produce that, but the wire format has to refuse it.
    for name, manifest in manifests.items():
        if set(manifest.cohort_inventory) != set(bundle.cohorts):
            raise CalibrationLakeError(
                f"calibration bundle dataset publishes other cohorts than the bundle: {name}"
            )
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


@dataclass(frozen=True, slots=True)
class _PublishedBuild:
    """A dataset build that exists but is not yet named by any published pointer."""

    reference: CalibrationDatasetRef
    manifest: DatasetManifest


def _published_build(root: Path, dataset: str, manifest: DatasetManifest) -> _PublishedBuild:
    path = root / dataset_manifest_key(dataset=dataset, build_id=manifest.build_id)
    return _PublishedBuild(
        reference=CalibrationDatasetRef(
            dataset=dataset,
            build_id=manifest.build_id,
            manifest_key=path.relative_to(root).as_posix(),
            manifest_sha256=sha256_bytes(path.read_bytes()),
            rows=manifest.totals.rows,
        ),
        manifest=manifest,
    )


def _publish_bundle(
    root: Path,
    *,
    updated: Mapping[str, _PublishedBuild],
    assembled_by: str | None = None,
    forward_policy: ForwardObservationPolicy = DEFAULT_FORWARD_OBSERVATION_POLICY,
) -> CalibrationBundleRef:
    """Expose one generation made of the builds just written plus the ones carried.

    The builds this operation produced arrive as values. A dataset it did not touch is
    taken from the bundle the pointer currently names — which is the whole of what a
    store carries forward. Reading a separate per-dataset pointer instead would make the
    published generation and the writer's continuation two different states, and only
    one of them survives being copied into the next work generation.
    """

    carried = _fixed_bundle(root)
    manifests: dict[str, DatasetManifest] = {}
    references: dict[str, CalibrationDatasetRef] = {}
    for name, dataset in L2_DATASETS.items():
        build = updated.get(name)
        if build is None:
            if carried is None:
                raise CalibrationLakeError(f"bundle dataset has no published build: {name}")
            build = _PublishedBuild(
                reference=carried.manifest.datasets[name],
                manifest=carried.datasets[name],
            )
        manifests[name] = build.manifest
        require_build_inputs(
            build.manifest,
            dataset=dataset,
            cache_schema_version=CACHE_SCHEMA_VERSIONS[name],
            forward_policy=forward_policy,
        )
        references[name] = build.reference
    cohort_keys = set(manifests[CALIBRATION_PANEL.name].cohort_inventory)
    if set(manifests[CALIBRATION_DIAGNOSTICS.name].cohort_inventory) != cohort_keys:
        raise CalibrationLakeError("panel and diagnostics cohort inventory differ")
    if set(manifests[CALIBRATION_FORWARD.name].cohort_inventory) != cohort_keys:
        raise CalibrationLakeError("panel and forward cohort inventory differ")
    cohorts = {
        asof: CalibrationCohortInventory(
            panel=manifests[CALIBRATION_PANEL.name].cohort_inventory[asof],
            diagnostics=manifests[CALIBRATION_DIAGNOSTICS.name].cohort_inventory[asof],
            forward=manifests[CALIBRATION_FORWARD.name].cohort_inventory[asof],
        )
        for asof in sorted(cohort_keys)
    }
    _require_one_measurement_policy(cohorts)
    now = datetime.now(UTC)
    bundle_id = f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}"
    # Assembling a bundle is a different act from producing a dataset, so it carries a
    # different identity. Requiring one shared commit instead would either stop a
    # forward-only maturation run whose panels were built at an earlier commit, or
    # restate a carried dataset's producer as the commit that merely republished it.
    bundle_manifest = CalibrationBundleManifest(
        bundle_id=bundle_id,
        created_at=now,
        assembled_by_git_commit=assembled_by or verified_git_commit(),
        forward_observation_policy=ForwardObservationPolicyRef(
            use_control_event_exits=forward_policy.use_control_event_exits
        ),
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
    previous = _pointer_ref(root)
    write_bytes_atomic(
        pointer_path,
        canonical_manifest_bytes(CalibrationBundlePointer(current=reference, previous=previous)),
    )
    return reference


def _require_one_measurement_policy(
    cohorts: Mapping[str, CalibrationCohortInventory],
) -> None:
    """One published generation is one series, measured one way throughout.

    Cohorts screened under different rules answer different questions, so aggregating
    them reports a difference in the rules as a difference in the market. The consumer
    side already refuses to evaluate a mixture, but that leaves the mixture publishable
    and only caught by whoever reads it next — the generation that creates it is where
    it can still be declined.
    """

    distinct = {
        entry.measurement_policy
        for cohort in cohorts.values()
        for entry in (cohort.panel, cohort.diagnostics, cohort.forward)
    }
    if len(distinct) > 1:
        stated = ", ".join(
            sorted(f"{policy.panel_variant}/{policy.rules_hash}" for policy in distinct)
        )
        raise CalibrationLakeError(f"calibration bundle mixes measurement policies: {stated}")


def current_bundle_ref(root: Path) -> CalibrationBundleRef | None:
    fixed = _fixed_bundle(root)
    return None if fixed is None else fixed.ref


def resolve_calibration_bundle(root: Path) -> FixedCalibrationBundle:
    fixed = _fixed_bundle(root)
    if fixed is None:
        raise CalibrationCacheError("calibration bundle is absent")
    return fixed


@dataclass(frozen=True, slots=True)
class BundleAdoptionReport:
    """What making a generation current actually cost, in bytes and in files."""

    bundle_id: str
    closure_objects: int
    hashed_bytes: int
    installed_objects: int
    installed_bytes: int
    reused_objects: int


def _bundle_closure_keys(root: Path, bundle: FixedCalibrationBundle) -> tuple[str, ...]:
    """Every stored key one bundle depends on, resolved through its manifests.

    A bundle is not the files that happen to sit under a directory. It is the manifest
    the pointer names, the dataset manifests that manifest names, the partition objects
    those enumerate, and the retained sources their cohorts were built from. A directory
    also holds builds this bundle has already passed, so installing a tree installs
    history; installing this set installs the generation.
    """

    keys: list[str] = [bundle.ref.manifest_key]
    for name, reference in sorted(bundle.manifest.datasets.items()):
        keys.append(reference.manifest_key)
        manifest = bundle.datasets[name]
        keys.extend(item.key for partition in manifest.partitions for item in partition.objects)
        for cohort in manifest.cohort_inventory.values():
            for source in retained_sources(cohort.sources):
                keys.append(source.key)
                input_manifest = load_lake_model_json(
                    (root / source.key).read_bytes(), CalibrationInputManifest
                )
                keys.extend(item.key for item in input_manifest.files.values())
    return tuple(dict.fromkeys(keys))


def _require_bundle_closure(root: Path, bundle: FixedCalibrationBundle) -> None:
    """Every source a cohort states must still resolve, with its declared contents.

    Output objects are checked against their manifests elsewhere; this is about the
    other half of the graph. A cohort that has lost the input it was built from still
    reads back perfectly, so nothing on the read path would notice — the loss surfaces
    only when someone tries to reproduce, pin, or publish it, long after the generation
    that dropped it became current.
    """

    for name, manifest in sorted(bundle.datasets.items()):
        for asof, cohort in sorted(manifest.cohort_inventory.items()):
            for source in retained_sources(cohort.sources):
                try:
                    resolve_source_ref(root, source)
                except (OSError, ValueError) as exc:
                    raise CalibrationLakeError(
                        f"{name}: cohort {asof} source does not resolve: {exc}"
                    ) from exc


def adopt_bundle_generation(
    root: Path,
    generated_root: Path,
    *,
    expected_current: CalibrationBundleRef | None,
    lock_held: bool = False,
) -> BundleAdoptionReport:
    """Install a verified generation's closure and expose only its bundle pointer.

    Work is proportional to what the generation closes over rather than to what the
    store has ever held. The generation is built by hard-linking the store into it, so
    a carried object arrives already sharing an inode with the one in the store: those
    need no install and no comparison, because they are the same bytes in the literal
    sense. What remains to install is what this build actually produced.

    This is the only call that moves the canonical pointer, so it takes the writer lock
    itself rather than trusting its callers to, on the same terms as ``write_panel``.
    The compare-and-set against ``expected_current`` is a check followed by an install
    followed by a write: two callers that read the same current would both pass the
    check, both install, and the later write would win while the earlier one reported
    success. ``lock_held`` is for callers already inside a publication.
    """

    publication = nullcontext() if lock_held else lake_writer_lock(root)
    with publication:
        return _adopt_bundle_generation(root, generated_root, expected_current=expected_current)


def _adopt_bundle_generation(
    root: Path,
    generated_root: Path,
    *,
    expected_current: CalibrationBundleRef | None,
) -> BundleAdoptionReport:
    fixed = _fixed_bundle(generated_root)
    if fixed is None:
        raise CalibrationLakeError("generated calibration bundle is missing")
    # The generation carries the rules its forward rows were observed under inside the
    # manifest that is about to become current, so adoption does not restate them; it
    # only refuses a generation whose contract this build cannot serve.
    _require_current_contract(generated_root)
    _require_bundle_closure(generated_root, fixed)
    hashed_bytes = 0
    for name, manifest in fixed.datasets.items():
        dataset = require_l2_dataset(name)
        for partition in manifest.partitions:
            for item in partition.objects:
                _require_object(generated_root / item.key, dataset=dataset, item=item)
                hashed_bytes += item.bytes

    pointer_path = root / current_calibration_bundle_pointer_key()
    actual = _pointer_ref(root)
    if actual != expected_current:
        raise CalibrationLakeError("calibration bundle moved while force build was in flight")

    installed_objects = 0
    installed_bytes = 0
    reused_objects = 0
    closure = _bundle_closure_keys(generated_root, fixed)
    for key in closure:
        source = generated_root / key
        target = root / key
        if _is_same_file(source, target):
            reused_objects += 1
            continue
        install_immutable_file(target, source, expected_sha256=sha256_file(source))
        installed_objects += 1
        installed_bytes += source.stat().st_size

    write_bytes_atomic(
        pointer_path,
        canonical_manifest_bytes(CalibrationBundlePointer(current=fixed.ref, previous=actual)),
    )
    return BundleAdoptionReport(
        bundle_id=fixed.ref.bundle_id,
        closure_objects=len(closure),
        hashed_bytes=hashed_bytes,
        installed_objects=installed_objects,
        installed_bytes=installed_bytes,
        reused_objects=reused_objects,
    )


def _is_same_file(source: Path, target: Path) -> bool:
    """Whether both names already refer to one inode, so there is nothing to install."""
    try:
        return source.stat().st_ino == target.stat().st_ino and (
            source.stat().st_dev == target.stat().st_dev
        )
    except OSError:
        return False


def _publish_cohort(
    root: Path,
    *,
    dataset: L2Dataset,
    asof: date,
    rows: Sequence[object],
    status: CohortStatus | None = None,
    source: CohortSourceRef,
    input_cutoff: date,
    measurement_policy: MeasurementPolicyRef,
    producer_commit: str | None = None,
    forward_policy: ForwardObservationPolicy = DEFAULT_FORWARD_OBSERVATION_POLICY,
) -> _PublishedBuild:
    """Publish a build that carries every cohort already published plus this one.

    A month can hold more than one cohort, so the partition being replaced is
    rebuilt from the rows the current build holds for the other as-ofs plus the new
    ones. Content addressing makes the untouched months resolve to the objects that
    are already stored.
    """

    root.mkdir(parents=True, exist_ok=True)
    month = (asof.year, asof.month)
    manifest = _current_manifest(root, dataset)
    if manifest is not None:
        # Carrying a partition from a build made under other measurement rules would
        # publish it under this build's fingerprint, which is exactly the mixing the
        # cohort contract exists to prevent. The version stamp is written only after
        # this passes, so a refused build leaves the store describing itself truthfully.
        require_build_inputs(
            manifest,
            dataset=dataset,
            cache_schema_version=CACHE_SCHEMA_VERSIONS[dataset.name],
            forward_policy=forward_policy,
        )
    inputs = _inputs(
        root,
        dataset,
        source,
        producer_commit=producer_commit,
        forward_policy=forward_policy,
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
    cohort_status: CohortStatus = status or ("complete" if rows else "empty")
    inventory[asof.isoformat()] = CohortInventoryEntry(
        status=cohort_status,
        rows=len(rows),
        sources=inputs.sources,
        input_cutoff=input_cutoff,
        measurement_policy=measurement_policy,
    )
    replacement_asofs = {
        str(cast(PanelRow | PanelDiagnostics | ForwardReturnRow, payload).asof)
        for payload in [*same_month, *rows]
    }
    replacement_sources = {
        (item.kind, item.source_id, item.sha256): item
        for cohort_asof in replacement_asofs
        for item in inventory[cohort_asof].sources
    }
    partition_inputs = L2BuildInputs(
        sources=tuple(replacement_sources[key] for key in sorted(replacement_sources)),
        producer_git_commit=inputs.producer_git_commit,
        cache_schema_version=inputs.cache_schema_version,
        forward_policy=inputs.forward_policy,
    )
    replacement = write_l2_partition(
        dataset=dataset,
        mirror_root=root,
        month=month,
        rows=[*same_month, *rows],
        inputs=partition_inputs,
    )
    if replacement is not None:
        carried.append(replacement)
    fingerprint = transform_fingerprint(
        dataset,
        cache_schema_version=CACHE_SCHEMA_VERSIONS[dataset.name],
        forward_policy=forward_policy,
    )
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
    return _published_build(root, dataset.name, report.manifest)


def _materialize(dataset: L2Dataset, payload: Mapping[str, object]) -> object:
    if dataset.name == CALIBRATION_PANEL.name:
        return panel_row_from_mapping(payload)
    if dataset.name == CALIBRATION_DIAGNOSTICS.name:
        return PanelDiagnostics(**payload)  # type: ignore[arg-type]
    return forward_row_from_mapping(payload)


def measurement_policy_of(diagnostics: PanelDiagnostics) -> MeasurementPolicyRef:
    """The rules identity a cohort's manifest entry states, taken from its own panel."""
    return MeasurementPolicyRef(
        rules_hash=diagnostics.rules_hash,
        panel_variant=diagnostics.panel_variant,
        production_authority=diagnostics.production_authority,
    )


def write_panel(
    root: Path,
    asof: date,
    rows: tuple[PanelRow, ...],
    diagnostics: PanelDiagnostics,
    *,
    source: CohortSourceRef,
    input_cutoff: date,
    producer_commit: str | None = None,
    lock_held: bool = False,
    forward_policy: ForwardObservationPolicy = DEFAULT_FORWARD_OBSERVATION_POLICY,
) -> None:
    measurement_policy = measurement_policy_of(diagnostics)
    publication = nullcontext() if lock_held else lake_writer_lock(root)
    with _store_errors(), publication:
        updated = {
            CALIBRATION_PANEL.name: _publish_cohort(
                root,
                dataset=CALIBRATION_PANEL,
                asof=asof,
                rows=rows,
                source=source,
                input_cutoff=input_cutoff,
                measurement_policy=measurement_policy,
                producer_commit=producer_commit,
                forward_policy=forward_policy,
            ),
            CALIBRATION_DIAGNOSTICS.name: _publish_cohort(
                root,
                dataset=CALIBRATION_DIAGNOSTICS,
                asof=asof,
                rows=(diagnostics,),
                source=source,
                input_cutoff=input_cutoff,
                measurement_policy=measurement_policy,
                producer_commit=producer_commit,
                forward_policy=forward_policy,
            ),
        }
        # Writing a panel always resets its outcome half, including when one is already
        # published. A forward cohort observes the names that panel selected, so a panel
        # rewritten for the same as-of — a corrected ticker set, a changed rank — leaves
        # the stored outcomes describing a cross-section that is no longer there. Rules
        # identity does not catch it, because the same rules over corrected inputs select
        # a different set. The producer that rewrites the panel is what recomputes the
        # forward; until it does, the cohort reads as `not_computed` rather than as an
        # outcome someone else measured.
        updated[CALIBRATION_FORWARD.name] = _publish_cohort(
            root,
            dataset=CALIBRATION_FORWARD,
            asof=asof,
            rows=(),
            status="not_computed",
            source=source,
            input_cutoff=input_cutoff,
            measurement_policy=measurement_policy,
            producer_commit=producer_commit,
            forward_policy=forward_policy,
        )
        _publish_bundle(
            root,
            updated=updated,
            assembled_by=producer_commit,
            forward_policy=forward_policy,
        )


def write_forward(
    root: Path,
    asof: date,
    rows: list[ForwardReturnRow],
    *,
    source: CohortSourceRef,
    input_cutoff: date,
    producer_commit: str | None = None,
    lock_held: bool = False,
    forward_policy: ForwardObservationPolicy = DEFAULT_FORWARD_OBSERVATION_POLICY,
) -> None:
    publication = nullcontext() if lock_held else lake_writer_lock(root)
    with _store_errors(), publication:
        # The outcome inherits the panel's rules identity rather than restating it: the
        # rows it observes are the names that panel selected, so a forward cohort that
        # claimed different rules would be describing a cross-section it did not use.
        build = _publish_cohort(
            root,
            dataset=CALIBRATION_FORWARD,
            asof=asof,
            rows=rows,
            source=source,
            input_cutoff=input_cutoff,
            measurement_policy=_panel_measurement_policy(root, asof),
            producer_commit=producer_commit,
            forward_policy=forward_policy,
        )
        _publish_bundle(
            root,
            updated={CALIBRATION_FORWARD.name: build},
            assembled_by=producer_commit,
            forward_policy=forward_policy,
        )


def _panel_measurement_policy(root: Path, asof: date) -> MeasurementPolicyRef:
    manifest = _current_manifest(root, CALIBRATION_PANEL)
    entry = None if manifest is None else manifest.cohort_inventory.get(asof.isoformat())
    if entry is None:
        raise CalibrationLakeError(
            f"calibration.forward: cohort {asof.isoformat()} has no panel to inherit rules from"
        )
    return entry.measurement_policy


@contextmanager
def _store_errors() -> Iterator[None]:
    """Present one error type at the store boundary, on the write side too.

    The read side already translates, and a caller that handles a failed read but
    receives a raw lower-layer exception from a failed write ends up as a traceback
    instead of the message that tells the operator to rebuild.
    """

    try:
        yield
    except (CalibrationLakeError, LakeRetentionError, OSError, ValueError) as exc:
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
        # The generation whose inventory answered is the one whose rows are read: this
        # is one question, so it may only see one bundle.
        return bool(_cohort_payloads(root, CALIBRATION_PANEL, asof, bundle=bundle))
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


def pointed_cohorts(root: Path) -> list[date] | None:
    """The cohorts the current pointer names, without closing the rest of the graph.

    A repair runs because the generation the pointer names does not resolve, which is
    exactly when a build about to replace it needs to know what was being served. Most
    of the ways a generation stops resolving — a dataset manifest whose digest moved, an
    object that disappeared, an inventory that disagrees with the bundle — leave the
    pointer and the bundle manifest it names intact, and those two state the served
    inventory exactly, digest-pinned, with no discovery involved.

    ``None`` means the store cannot state it: the pointer bytes or the manifest they
    name are themselves unreadable. Only then does the answer have to be inferred.
    """

    pointer_path = root / current_calibration_bundle_pointer_key()
    try:
        pointer = load_lake_model_json(pointer_path.read_bytes(), CalibrationBundlePointer)
        payload = (root / pointer.current.manifest_key).read_bytes()
        if sha256_bytes(payload) != pointer.current.manifest_sha256:
            return None
        manifest = load_lake_model_json(payload, CalibrationBundleManifest)
    except (OSError, ValueError):
        return None
    return sorted(
        date.fromisoformat(asof)
        for asof, entry in manifest.cohorts.items()
        if entry.panel.status in {"complete", "empty"}
    )


def orphaned_cohorts(root: Path) -> list[date]:
    """As-ofs the store may have served, inferred from bundle manifests on disk.

    This is the answer of last resort, for a store whose pointer states nothing and
    whose manifests are all that is left — a second repair attempt, or a pointer whose
    own bytes are corrupt. Each manifest states its own cohort set, so the union of the
    readable ones covers what any generation published.

    It is an inference, not the inventory. A union over generations names cohorts a
    later generation may have dropped on purpose, and a manifest this code cannot decode
    contributes nothing at all. Both errors are tolerable only for the use it has: as a
    floor a destructive rebuild must clear. Over-refusing sends the operator to a
    separate directory, which the refusal names; under-refusing needs the current
    manifest itself to be gone, which is loss the pointer already told them about.
    """

    asofs: set[date] = set()
    for path in sorted(_bundle_manifest_dir(root).glob("*.json")):
        try:
            manifest = load_lake_model_json(path.read_bytes(), CalibrationBundleManifest)
            asofs.update(
                date.fromisoformat(asof)
                for asof, entry in manifest.cohorts.items()
                if entry.panel.status in {"complete", "empty"}
            )
        except (OSError, ValueError):
            # A manifest this code cannot decode says nothing about the inventory, and a
            # repair must not be blocked by the leftovers of a failed publication.
            continue
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
    # Both halves of "can this code decode these rows", asked about this dataset only:
    # the published contract version, and the transform that produced the values under
    # it. Resolution deliberately asks neither, so this is where a generation whose other
    # dataset moved on stops being anyone else's problem.
    require_manifest_contract(dataset, manifest)
    require_build_inputs(
        manifest, dataset=dataset, cache_schema_version=CACHE_SCHEMA_VERSIONS[dataset.name]
    )
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
    try:
        fixed = _read_generation(root, bundle)
        payloads = _cohort_payloads(root, CALIBRATION_PANEL, asof, bundle=fixed)
    except (CalibrationLakeError, LakeRetentionError) as exc:
        raise CalibrationCacheError(f"calibration panel cache is invalid: {exc}") from exc
    entry = fixed.manifest.cohorts.get(asof.isoformat())
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
    try:
        fixed = _read_generation(root, bundle)
        payloads = _cohort_payloads(root, CALIBRATION_DIAGNOSTICS, asof, bundle=fixed)
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
    try:
        fixed = _read_generation(root, bundle)
        # The observation rules come from the generation being read, not from whatever
        # the pointer names now. Taking them from current would validate this bundle's
        # forward build against another generation's policy.
        forward_policy = _policy_of(fixed)
        manifest = fixed.datasets[CALIBRATION_FORWARD.name]
        cohort = fixed.manifest.cohorts.get(asof.isoformat())
        if cohort is None or cohort.forward.status in {"partial", "not_computed"}:
            raise CalibrationCacheError(
                "calibration cache is partial; run calibration-build --force"
            )
        require_manifest_contract(CALIBRATION_FORWARD, manifest)
        require_build_inputs(
            manifest,
            dataset=CALIBRATION_FORWARD,
            cache_schema_version=CACHE_SCHEMA_VERSIONS[CALIBRATION_FORWARD.name],
            forward_policy=forward_policy,
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
