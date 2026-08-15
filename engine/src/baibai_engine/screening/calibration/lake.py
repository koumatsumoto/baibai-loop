"""Typed Parquet L2 builds for the calibration panel, diagnostics, and forward rows.

The calibration cohort is a wide, append-only cross-section that is rebuilt rather
than edited, which is what makes it an L2 dataset rather than a table. Each build is
immutable and carries the identity it was produced from: the L1 release it was bound
to, the legacy store it still had to read, the code that produced it, and the
fingerprint of the transform. A contract change is a new build under a new
``contract_version``, never an edit of published objects.

The Arrow schema is derived from the row dataclasses, so the stored columns cannot
drift from the contract the rest of calibration computes against — a field added to
``PanelRow`` changes the schema, the transform fingerprint, and therefore the build.

Reads are fail-closed. A build whose schema, transform fingerprint, or source release
is not the one the reader expects is an error; there is no ``union_by_name`` and no
column-defaulting, because a silently missing column would read as "measured and
absent" for a cohort that never measured it at all.
"""

from __future__ import annotations

import ast
import hashlib
import json
import types
import typing
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, date, datetime
from functools import cache
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]
from pydantic import (
    BaseModel,
)

from baibai_engine.market.lake.immutable import ImmutableInstallError, install_immutable_bytes
from baibai_engine.market.lake.keys import (
    canonical_object_key,
    dataset_manifest_key,
    validate_identifier,
)
from baibai_engine.market.lake.models import (
    CalibrationBundleManifest,
    CalibrationBundlePointer,
    CalibrationBundleRef,
    CalibrationCohortInventory,
    CalibrationDatasetRef,
    CohortInventoryEntry,
    CohortSourceRef,
    DatasetManifest,
    LakeObject,
    ManifestTotals,
    PartitionManifest,
    load_lake_model_json,
)
from baibai_engine.market.lake.objects import sha256_bytes, sha256_file
from baibai_engine.market.lake.reader import schema_matches

from .forward import (
    DEFAULT_FORWARD_OBSERVATION_POLICY,
    ForwardObservationPolicy,
    ForwardReturnRow,
)
from .panel import PanelDiagnostics, PanelRow

__all__ = [
    "CalibrationBundleManifest",
    "CalibrationBundlePointer",
    "CalibrationBundleRef",
    "CalibrationCohortInventory",
    "CalibrationDatasetRef",
]

L2_CONTRACT_VERSION = 1
_ROW_GROUP_SIZE = 65_536
_PARQUET_VERSION = "2.6"
_COMPRESSION = "zstd"
_COMPRESSION_LEVEL = 9

# The columns that order rows inside a partition. Every calibration row is identified
# by its cohort and instrument; the forward dataset adds the horizon.
_IDENTITY_COLUMNS = ("asof", "ticker", "horizon")
PANEL_DATASET = "calibration.panel"
DIAGNOSTICS_DATASET = "calibration.panel_diagnostics"
FORWARD_DATASET = "calibration.forward"


class CalibrationLakeError(RuntimeError):
    """An L2 calibration build cannot be produced or cannot be read as published."""


@dataclass(frozen=True, slots=True)
class L2Dataset:
    """One typed calibration dataset and the row type that defines its columns."""

    name: str
    row_type: type
    contract_version: int = L2_CONTRACT_VERSION
    partition_by: tuple[str, ...] = ("year", "month")
    primary_key: tuple[str, ...] = ("asof", "ticker")
    asof_field: str = "asof"

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in fields(self.row_type))

    @property
    def arrow_schema(self) -> Any:
        return _arrow_schema(self)


def _arrow_type(annotation: object, *, label: str) -> tuple[Any, bool]:
    """Map one dataclass annotation to an Arrow type and its nullability.

    Only the shapes the calibration rows actually use are accepted. An unmapped
    annotation raises instead of falling back to a string, because a column stored
    under the wrong type is a silent contract break that survives a round trip.
    """

    origin = get_origin(annotation)
    if origin is Literal:
        values = get_args(annotation)
        if not all(isinstance(value, str) for value in values):
            raise CalibrationLakeError(f"{label}: only string literals are storable")
        return pa.string(), False
    if origin in (types.UnionType, typing.Union):
        members = [item for item in get_args(annotation) if item is not type(None)]
        if len(members) != 1 or type(None) not in get_args(annotation):
            raise CalibrationLakeError(f"{label}: only optional single types are storable")
        inner, _nullable = _arrow_type(members[0], label=label)
        return inner, True
    if origin is dict:
        key_type, value_type = get_args(annotation)
        if key_type is not str or value_type is not int:
            raise CalibrationLakeError(f"{label}: only str->int maps are storable")
        return pa.map_(pa.string(), pa.int64()), False
    if annotation is str:
        return pa.string(), False
    if annotation is bool:
        return pa.bool_(), False
    if annotation is int:
        return pa.int64(), False
    if annotation is float:
        return pa.float64(), False
    raise CalibrationLakeError(f"{label}: unsupported annotation {annotation!r}")


def _arrow_schema(dataset: L2Dataset) -> Any:
    if not is_dataclass(dataset.row_type):
        raise CalibrationLakeError(f"{dataset.name}: row type must be a dataclass")
    hints = typing.get_type_hints(dataset.row_type)
    columns = []
    for field in fields(dataset.row_type):
        arrow_type, nullable = _arrow_type(hints[field.name], label=f"{dataset.name}.{field.name}")
        columns.append(pa.field(field.name, arrow_type, nullable=nullable))
    metadata = {
        b"baibai.contract_version": str(dataset.contract_version).encode(),
        b"baibai.dataset": dataset.name.encode(),
        b"baibai.row_type": dataset.row_type.__name__.encode(),
    }
    return pa.schema(columns, metadata=metadata)


CALIBRATION_PANEL = L2Dataset(name=PANEL_DATASET, row_type=PanelRow)
CALIBRATION_DIAGNOSTICS = L2Dataset(
    name=DIAGNOSTICS_DATASET, row_type=PanelDiagnostics, primary_key=("asof",)
)
CALIBRATION_FORWARD = L2Dataset(
    name=FORWARD_DATASET,
    row_type=ForwardReturnRow,
    primary_key=("asof", "ticker", "horizon"),
)

L2_DATASETS: Mapping[str, L2Dataset] = {
    dataset.name: dataset
    for dataset in (CALIBRATION_PANEL, CALIBRATION_DIAGNOSTICS, CALIBRATION_FORWARD)
}


@dataclass(frozen=True, slots=True)
class FixedCalibrationBundle:
    ref: CalibrationBundleRef
    manifest: CalibrationBundleManifest
    datasets: Mapping[str, DatasetManifest]


def require_l2_dataset(name: str) -> L2Dataset:
    try:
        return L2_DATASETS[name]
    except KeyError as exc:
        allowed = ", ".join(sorted(L2_DATASETS))
        raise CalibrationLakeError(
            f"unsupported L2 calibration dataset {name!r}; expected one of: {allowed}"
        ) from exc


def transform_fingerprint(
    dataset: L2Dataset,
    *,
    cache_schema_version: str,
    forward_policy: ForwardObservationPolicy = DEFAULT_FORWARD_OBSERVATION_POLICY,
) -> str:
    """Identify the logic, config, and observation rules behind a build.

    ``cache_schema_version`` is the calibration contract hash: it already covers the
    row fields, the relaxed thresholds a cohort was measured with, and the valuation
    revision. Folding it in means a cohort produced under different measurement rules
    cannot be mistaken for a rebuild of the same one.

    ``forward_policy`` states the runtime observation rules a forward cohort was
    measured under. It belongs to the forward dataset alone, so a comparison run that
    changes how exits are observed does not invalidate published panel months.
    """

    contract: dict[str, object] = {
        "arrow_schema": str(dataset.arrow_schema),
        "cache_schema_version": cache_schema_version,
        "compression": _COMPRESSION,
        "compression_level": _COMPRESSION_LEVEL,
        "contract_version": dataset.contract_version,
        "dataset": dataset.name,
        "parquet_version": _PARQUET_VERSION,
        "partition_by": dataset.partition_by,
        "row_group_size": _ROW_GROUP_SIZE,
        "writer": f"pyarrow-{pa.__version__}",
        "implementation_sha256": _semantic_implementation_digests(dataset),
    }
    if dataset.name == FORWARD_DATASET:
        contract["forward_observation_policy"] = forward_policy.digest
    payload = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


# What a row's value, status, and membership are decided by is whatever the build
# imports, transitively. Naming those modules by hand is how the set falls behind: a
# helper that a listed module starts importing changes rows while the fingerprint says
# nothing changed, and nothing in the repository notices. So the closure is derived
# from the import graph of the module that produces the dataset, and a new dependency
# joins it by being imported.
#
# The two dataset entry points are separate, so a change to how a forward outcome is
# observed does not invalidate published panel months — panel reaches ``forward`` only
# for the one constant it borrows, and ``forward`` does not reach panel at all.
_DATASET_ENTRY_MODULES = {
    PANEL_DATASET: "screening/calibration/panel.py",
    DIAGNOSTICS_DATASET: "screening/calibration/panel.py",
    FORWARD_DATASET: "screening/calibration/forward.py",
}

# The writer decides the physical shape a row is stored in rather than its value, and
# it imports both dataset modules for their types. Following its imports would tie the
# two datasets together through a dependency that cannot move a number, so these two
# are hashed as files and their imports are not followed.
_WRITER_MODULES = (
    "screening/calibration/lake.py",
    "screening/calibration/store.py",
)

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_ENGINE_PACKAGE = "baibai_engine"


def _module_path(name: str) -> Path | None:
    """The file a dotted engine module names, or ``None`` when it is outside the package."""
    parts = name.split(".")
    if parts[0] != _ENGINE_PACKAGE:
        return None
    candidate = _ENGINE_ROOT.joinpath(*parts[1:])
    module = candidate.with_suffix(".py")
    if module.is_file():
        return module
    package = candidate / "__init__.py"
    return package if package.is_file() else None


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(_ENGINE_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join([_ENGINE_PACKAGE, *parts])


def _imported_modules(path: Path) -> set[str]:
    """Every engine module this file could bind, absolute and relative forms alike.

    ``from x import y`` is recorded as both ``x`` and ``x.y`` because the name may be a
    submodule or a symbol inside one, and only the filesystem can tell which.
    """

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        raise CalibrationLakeError(f"semantic dependency is unreadable: {path}") from exc
    package = _module_name(path)
    if path.name != "__init__.py":
        package = package.rsplit(".", 1)[0]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                anchor = base[: len(base) - node.level + 1]
                module = ".".join([*anchor, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


@cache
def _semantic_closure(entry: str) -> tuple[Path, ...]:
    """Every engine module reachable by import from one dataset's producer."""

    start = _ENGINE_ROOT / entry
    if not start.is_file():
        raise CalibrationLakeError(f"semantic entry module is missing: {entry}")
    reached: set[Path] = set()
    pending = [start]
    while pending:
        path = pending.pop()
        if path in reached:
            continue
        reached.add(path)
        pending.extend(
            resolved
            for name in _imported_modules(path)
            if (resolved := _module_path(name)) is not None
        )
    return tuple(sorted(reached))


def _semantic_implementation_digests(dataset: L2Dataset) -> dict[str, str]:
    paths = set(_semantic_closure(_DATASET_ENTRY_MODULES[dataset.name]))
    for name in _WRITER_MODULES:
        writer = _ENGINE_ROOT / name
        if not writer.is_file():
            raise CalibrationLakeError(f"semantic dependency is missing: {name}")
        paths.add(writer)
    return {path.relative_to(_ENGINE_ROOT).as_posix(): sha256_file(path) for path in sorted(paths)}


@dataclass(frozen=True, slots=True)
class L2BuildInputs:
    """The exact source generation and implementation identity for one cohort write."""

    sources: tuple[CohortSourceRef, ...]
    producer_git_commit: str
    cache_schema_version: str
    forward_policy: ForwardObservationPolicy = DEFAULT_FORWARD_OBSERVATION_POLICY


@dataclass(frozen=True, slots=True)
class L2BuildReport:
    dataset: str
    build_id: str
    manifest_path: Path
    manifest: DatasetManifest
    partitions: tuple[str, ...]
    rows: int


def asof_month(asof: str) -> tuple[int, int]:
    value = date.fromisoformat(asof)
    return (value.year, value.month)


def write_l2_partition(
    *,
    dataset: L2Dataset,
    mirror_root: Path,
    month: tuple[int, int],
    rows: Sequence[object],
    inputs: L2BuildInputs,
) -> PartitionManifest | None:
    """Write one month as a single deterministic object, or nothing when it is empty.

    A partition is the unit of rebuild: a cohort that changes rewrites its own month
    and leaves every other month's published object exactly where it is. Rewriting
    the whole dataset for one cohort would cost the dataset's full size on every
    build, which is the transfer amplification this design exists to remove.
    """

    names = dataset.field_names
    payloads = [{name: getattr(row, name) for name in names} for row in rows]
    if not payloads:
        return None
    # Sort before writing so a partition's bytes depend on its rows, not on the order
    # the cohorts that share the month happened to be published in.
    ordering = dataset.primary_key
    if any(name not in names for name in ordering) or dataset.asof_field not in names:
        raise CalibrationLakeError(f"{dataset.name}: row identity is outside the row contract")
    identities: list[tuple[str, ...]] = []
    for payload in payloads:
        if any(payload[name] is None for name in ordering):
            raise CalibrationLakeError(f"{dataset.name}: primary key cannot contain null")
        try:
            row_month = asof_month(str(payload[dataset.asof_field]))
        except ValueError as exc:
            raise CalibrationLakeError(f"{dataset.name}: row as-of is invalid") from exc
        if row_month != month:
            raise CalibrationLakeError(f"{dataset.name}: row escapes its year/month partition")
        identities.append(tuple(str(payload[name]) for name in ordering))
    paired = sorted(zip(identities, payloads, strict=True), key=lambda item: item[0])
    if any(left[0] == right[0] for left, right in pairwise(paired)):
        raise CalibrationLakeError(f"{dataset.name}: duplicate primary key")
    identities = [item[0] for item in paired]
    payloads = [item[1] for item in paired]
    table = pa.Table.from_pylist(payloads, schema=dataset.arrow_schema)
    digest, size, _path = _write_object(
        table, dataset=dataset, month=month, mirror_root=mirror_root
    )
    key = canonical_object_key(
        layer="l2_analytical",
        dataset=dataset.name,
        contract_version=dataset.contract_version,
        partition_values={"year": month[0], "month": month[1]},
        partition_by=dataset.partition_by,
        content_sha256=digest,
    )
    return PartitionManifest(
        values={"year": month[0], "month": month[1]},
        objects=(
            LakeObject(
                key=key,
                sha256=digest,
                bytes=size,
                rows=len(payloads),
                min_key=identities[0],
                max_key=identities[-1],
            ),
        ),
        sources=(),
        # The state this partition was produced from is the set of cohorts it holds
        # under one input generation. A constant here would make two partitions built
        # from different inputs indistinguishable in the manifest.
        source_state_sha256=sha256_bytes(
            json.dumps(
                {
                    "sources": [
                        {
                            "kind": source.kind,
                            "source_id": source.source_id,
                            "sha256": source.sha256,
                        }
                        for source in inputs.sources
                    ],
                    "asofs": sorted({str(payload["asof"]) for payload in payloads}),
                    "rows": len(payloads),
                    "content": digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ),
    )


def publish_l2_build(
    *,
    dataset: L2Dataset,
    mirror_root: Path,
    partitions: Sequence[PartitionManifest],
    inputs: L2BuildInputs,
    build_id: str,
    data_as_of: date,
    cohort_inventory: Mapping[str, CohortInventoryEntry],
    created_at: datetime | None = None,
) -> L2BuildReport:
    """Fix a set of published partitions as one immutable build."""

    validate_identifier(build_id, label="build_id")
    now = (created_at or datetime.now(UTC)).astimezone(UTC)
    ordered = tuple(
        sorted(partitions, key=lambda item: (int(item.values["year"]), int(item.values["month"])))
    )
    manifest = DatasetManifest(
        manifest_version=1,
        dataset=dataset.name,
        layer="l2_analytical",
        contract_version=dataset.contract_version,
        build_id=build_id,
        sources=(),
        producer_git_commit=inputs.producer_git_commit,
        transform_fingerprint=transform_fingerprint(
            dataset,
            cache_schema_version=inputs.cache_schema_version,
            forward_policy=inputs.forward_policy,
        ),
        created_at=now,
        coverage_start=min(
            (date.fromisoformat(value) for value in cohort_inventory), default=data_as_of
        ),
        data_as_of=data_as_of,
        population_count=sum(item.rows for item in cohort_inventory.values()),
        coverage_status=(
            "partial"
            if any(item.status in {"partial", "not_computed"} for item in cohort_inventory.values())
            else "complete"
        ),
        partition_by=dataset.partition_by,
        cohort_inventory=cohort_inventory,
        partitions=ordered,
        totals=ManifestTotals(
            objects=sum(len(item.objects) for item in ordered),
            bytes=sum(obj.bytes for item in ordered for obj in item.objects),
            rows=sum(obj.rows for item in ordered for obj in item.objects),
        ),
    )
    manifest_path = mirror_root / dataset_manifest_key(dataset=dataset.name, build_id=build_id)
    _write_immutable(manifest_path, canonical_manifest_bytes(manifest))
    return L2BuildReport(
        dataset=dataset.name,
        build_id=build_id,
        manifest_path=manifest_path,
        manifest=manifest,
        partitions=tuple(
            f"{int(item.values['year']):04d}-{int(item.values['month']):02d}" for item in ordered
        ),
        rows=manifest.totals.rows,
    )


def _write_object(
    table: Any, *, dataset: L2Dataset, month: tuple[int, int], mirror_root: Path
) -> tuple[str, int, Path]:
    """Write one deterministic Parquet object and install it at its content key."""

    staging = mirror_root / "lake" / "staging" / dataset.name
    staging.mkdir(parents=True, exist_ok=True)
    provisional = staging / (f"{month[0]:04d}-{month[1]:02d}.{uuid.uuid4().hex}.parquet")
    pq.write_table(
        table,
        provisional,
        compression=_COMPRESSION,
        compression_level=_COMPRESSION_LEVEL,
        data_page_version="1.0",
        row_group_size=_ROW_GROUP_SIZE,
        use_dictionary=False,
        version=_PARQUET_VERSION,
        write_statistics=True,
    )
    digest = sha256_file(provisional)
    size = provisional.stat().st_size
    key = canonical_object_key(
        layer="l2_analytical",
        dataset=dataset.name,
        contract_version=dataset.contract_version,
        partition_values={"year": month[0], "month": month[1]},
        partition_by=dataset.partition_by,
        content_sha256=digest,
    )
    target = mirror_root / key
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if sha256_file(target) != digest:
            raise CalibrationLakeError(f"content-addressed key collision: {key}")
        provisional.unlink()
    else:
        provisional.replace(target)
    return digest, size, target


def read_l2_partition(
    *,
    dataset: L2Dataset,
    manifest: DatasetManifest,
    mirror_root: Path,
    month: tuple[int, int],
    columns: Sequence[str] | None = None,
) -> list[dict[str, object]]:
    """Read one month of a published build, verifying identity before any row.

    ``maps_as_pydicts`` is what keeps a map column the shape the row declares. Arrow
    hands map columns back as key/value pairs by default, and a consumer that asks
    ``isinstance(value, dict)`` would then read a populated column as empty — the
    measurement silently becoming "observed and zero".

    ``columns`` narrows the decode. A caller that only needs the as-of should not pay
    to turn every column of every row into a Python object.
    """

    require_manifest_contract(dataset, manifest)
    selected = None if columns is None else list(columns)
    for partition in manifest.partitions:
        if (int(partition.values["year"]), int(partition.values["month"])) != month:
            continue
        rows: list[dict[str, object]] = []
        previous_key: tuple[str, ...] | None = None
        for item in partition.objects:
            path = mirror_root / item.key
            _require_object(path, dataset=dataset, item=item)
            table = pq.read_table(path, columns=selected)
            decoded = table.to_pylist(maps_as_pydicts="strict")
            if selected is None or set(dataset.primary_key).issubset(selected):
                identities = [
                    tuple(str(payload[name]) for name in dataset.primary_key) for payload in decoded
                ]
                if identities and (identities[0] != item.min_key or identities[-1] != item.max_key):
                    raise CalibrationLakeError(
                        f"{dataset.name}: object primary-key range differs: {item.key}"
                    )
                if identities != sorted(identities) or len(identities) != len(set(identities)):
                    raise CalibrationLakeError(
                        f"{dataset.name}: object primary keys are not unique and ordered: "
                        f"{item.key}"
                    )
                if previous_key is not None and identities and previous_key >= identities[0]:
                    raise CalibrationLakeError(f"{dataset.name}: object primary-key ranges overlap")
                if identities:
                    previous_key = identities[-1]
                for payload in decoded:
                    if asof_month(str(payload[dataset.asof_field])) != month:
                        raise CalibrationLakeError(
                            f"{dataset.name}: row escapes its manifest partition"
                        )
            rows.extend(decoded)
        return rows
    return []


def require_manifest_contract(dataset: L2Dataset, manifest: DatasetManifest) -> None:
    if manifest.dataset != dataset.name:
        raise CalibrationLakeError(f"manifest is for {manifest.dataset}, not {dataset.name}")
    if manifest.layer != "l2_analytical":
        raise CalibrationLakeError(f"{dataset.name}: manifest is not an L2 analytical build")
    if manifest.contract_version != dataset.contract_version:
        raise CalibrationLakeError(
            f"{dataset.name} publishes contract v{manifest.contract_version}; "
            f"this reader accepts only v{dataset.contract_version}"
        )
    if tuple(manifest.partition_by) != dataset.partition_by:
        raise CalibrationLakeError(f"{dataset.name}: manifest partition layout is unexpected")


def require_build_inputs(
    manifest: DatasetManifest,
    *,
    dataset: L2Dataset,
    cache_schema_version: str,
    forward_policy: ForwardObservationPolicy = DEFAULT_FORWARD_OBSERVATION_POLICY,
    source_release_id: str | None = None,
) -> None:
    """Fail closed when a build was produced by a different transform, policy, or input."""

    expected = transform_fingerprint(
        dataset,
        cache_schema_version=cache_schema_version,
        forward_policy=forward_policy,
    )
    if manifest.transform_fingerprint != expected:
        raise CalibrationLakeError(
            f"{dataset.name}: build {manifest.build_id} was produced by a different transform; "
            "rebuild it"
        )
    cohort_sources = tuple(
        source for cohort in manifest.cohort_inventory.values() for source in cohort.sources
    )
    if not cohort_sources:
        raise CalibrationLakeError(f"{dataset.name}: build {manifest.build_id} names no input")
    if source_release_id is not None and source_release_id not in {
        source.source_id for source in cohort_sources
    }:
        raise CalibrationLakeError(
            f"{dataset.name}: build {manifest.build_id} was not built from {source_release_id}"
        )


def _require_object(path: Path, *, dataset: L2Dataset, item: LakeObject) -> None:
    if not path.is_file():
        raise CalibrationLakeError(f"{dataset.name}: published object is missing: {item.key}")
    if path.stat().st_size != item.bytes or sha256_file(path) != item.sha256:
        raise CalibrationLakeError(f"{dataset.name}: published object differs: {item.key}")
    try:
        schema = pq.read_schema(path)
        metadata = pq.read_metadata(path)
    except (OSError, ValueError) as exc:
        raise CalibrationLakeError(f"{dataset.name}: object is unreadable: {item.key}") from exc
    if not schema_matches(schema, dataset.arrow_schema):
        raise CalibrationLakeError(f"{dataset.name}: object schema is not the contract: {item.key}")
    if metadata.num_rows != item.rows:
        raise CalibrationLakeError(f"{dataset.name}: object row count differs: {item.key}")


def canonical_manifest_bytes(manifest: BaseModel) -> bytes:
    return (
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def load_manifest(path: Path) -> DatasetManifest:
    try:
        return load_lake_model_json(path.read_bytes(), DatasetManifest)
    except (OSError, ValueError) as exc:
        raise CalibrationLakeError(f"L2 dataset manifest is unreadable: {path}") from exc


def _write_immutable(path: Path, payload: bytes) -> None:
    try:
        install_immutable_bytes(path, payload)
    except ImmutableInstallError as exc:
        raise CalibrationLakeError(f"immutable manifest key already differs: {path}") from exc


def build_identifier(*, dataset: L2Dataset, fingerprint: str, now: datetime) -> str:
    """A build id that states when it was made and which transform made it.

    The random segment is what makes two builds published inside the same second
    distinct: build ids address immutable manifests, so a collision would be a
    write against a key that already holds different bytes.
    """

    return (
        f"{now:%Y%m%dT%H%M%SZ}-{dataset.name.replace('.', '-')}-"
        f"{uuid.uuid4().hex[:8]}-{fingerprint[-12:]}"
    )


def months_of(asofs: Iterable[str]) -> tuple[tuple[int, int], ...]:
    return tuple(sorted({asof_month(asof) for asof in asofs}))
