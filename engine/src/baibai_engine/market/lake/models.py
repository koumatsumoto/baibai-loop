"""Strict immutable manifest models for L1 and L2 market datasets."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from .datasets import LAKE_DATASETS
from .keys import (
    PartitionValue,
    calibration_bundle_manifest_key,
    canonical_object_key,
    dataset_manifest_key,
    raw_metadata_object_key,
    raw_object_key,
    release_manifest_key,
    validate_dataset_name,
    validate_identifier,
    validate_lake_object_key,
    validate_partition_layout,
    validate_sha256,
)

ManifestLayer = Literal["l1_canonical", "l2_analytical"]
CoverageStatus = Literal["complete", "partial"]
CohortStatus = Literal["complete", "empty", "partial", "not_computed"]
ReleaseProfile = Literal["production"]
MAX_LAKE_JSON_BYTES = 16 * 1024 * 1024
CALIBRATION_DATASETS = frozenset(
    {"calibration.panel", "calibration.panel_diagnostics", "calibration.forward"}
)


class RawArchiveMetadata(BaseModel):
    """Strict sidecar that binds provider Raw bytes to their retrieval identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    metadata_version: Literal[1]
    provider: str
    dataset: str
    ingest_id: str
    retrieved_at: datetime
    retention_class: Literal["preserve", "buffer"]
    suffix: Literal[".json.gz", ".csv.gz", ".zip"]
    endpoint: str | None = None
    request_start: date | None = None
    request_end: date | None = None
    object_key: str
    content_sha256: str
    bytes: int = Field(gt=0)

    @field_validator("provider", "dataset")
    @classmethod
    def validate_dataset_segment(cls, value: str) -> str:
        return validate_dataset_name(value)

    @field_validator("ingest_id")
    @classmethod
    def validate_ingest_id(cls, value: str) -> str:
        return validate_identifier(value, label="ingest_id")

    @field_validator("retrieved_at")
    @classmethod
    def validate_retrieved_at(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("retrieved_at must be UTC")
        return value

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("endpoint must not contain credentials, query, or fragment")
        return value

    @field_validator("content_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return validate_sha256(value)

    @model_validator(mode="after")
    def validate_identity(self) -> RawArchiveMetadata:
        expected = raw_object_key(
            provider=self.provider,
            dataset=self.dataset,
            ingest_date=self.retrieved_at.date(),
            ingest_id=self.ingest_id,
            suffix=self.suffix,
        )
        if self.object_key != expected:
            raise ValueError("Raw object_key does not match metadata identity")
        if (
            self.request_start is not None
            and self.request_end is not None
            and self.request_start > self.request_end
        ):
            raise ValueError("request_start must not be after request_end")
        return self


class _SourceRefBase(BaseModel):
    """What every lineage reference states: which generation, and its exact bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_id: str
    sha256: str

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        return validate_identifier(value, label="source_id")

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return validate_sha256(value)


class _RetainedSourceRefBase(_SourceRefBase):
    """A source the lake keeps: it resolves to a stored object and roots retention.

    ``key`` is the whole difference between the two families. A reference that names
    a key is a promise that those bytes are reachable, protected from collection, and
    carried by any publication that carries the build — so only sources whose size the
    lake is willing to hold for the life of the generation may name one.
    """

    key: str


class RawIngestSourceRef(_RetainedSourceRefBase):
    kind: Literal["raw_ingest"]
    provider: str
    dataset: str
    request_start: date
    request_end: date
    metadata_version: int = Field(ge=1)
    metadata_key: str
    metadata_sha256: str

    @field_validator("provider", "dataset")
    @classmethod
    def validate_source_segment(cls, value: str) -> str:
        return validate_dataset_name(value)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        key = validate_lake_object_key(value)
        if key.startswith("lake/l1/raw/legacy_sqlite/") or not (
            key.startswith("lake/l1/raw/") and key.endswith((".csv.gz", ".json.gz", ".zip"))
        ):
            raise ValueError("raw_ingest must reference an L1 Raw object")
        return key

    @field_validator("metadata_key")
    @classmethod
    def validate_metadata_key(cls, value: str) -> str:
        return validate_lake_object_key(value)

    @field_validator("metadata_sha256")
    @classmethod
    def validate_metadata_digest(cls, value: str) -> str:
        return validate_sha256(value)

    @model_validator(mode="after")
    def validate_identity(self) -> RawIngestSourceRef:
        if self.request_start > self.request_end:
            raise ValueError("raw_ingest request_start must not be after request_end")
        if PurePosixPath(self.key).name not in {
            f"{self.source_id}.csv.gz",
            f"{self.source_id}.json.gz",
            f"{self.source_id}.zip",
        }:
            raise ValueError("raw_ingest key does not match source_id")
        if self.metadata_key != raw_metadata_object_key(raw_key=self.key):
            raise ValueError("raw_ingest metadata key does not match Raw object key")
        return self


class SQLiteSnapshotSourceRef(_SourceRefBase):
    """Which sealed generation of the legacy store a build read, without keeping it.

    The snapshot exists to give one build a single consistent read of a store that
    changes under it. That job ends when the build ends, and the bytes are the whole
    legacy store — roughly 2 GB — so keeping one per build would make the lake grow
    with the number of runs rather than with what it publishes. This reference is
    therefore identity only: it names the schema version, the content digest, and the
    capture time, and it names no key, because nothing keeps those bytes.

    What it still proves: two builds that state the same ``source_id`` read identical
    input, and a store generation offered as the input to a rebuild can be checked
    against this digest before it is believed. What it does not promise: that such a
    generation is still obtainable. Rebuild-from-lineage returns as a guarantee when
    the tables a cohort needs are published as L1 releases (Issue #917), which is a
    rebuildable input the lake keeps by key.
    """

    kind: Literal["sqlite_snapshot"]
    schema_version: int = Field(ge=1)
    captured_at: datetime

    @field_validator("captured_at")
    @classmethod
    def validate_captured_at(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("captured_at must be UTC")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> SQLiteSnapshotSourceRef:
        if self.source_id != f"market-v{self.schema_version}-{self.sha256[:24]}":
            raise ValueError("sqlite_snapshot source_id does not match its schema and digest")
        return self


class L1ReleaseSourceRef(_RetainedSourceRefBase):
    """A digest-pinned reference to one L1 release, used to read a fixed generation.

    This is deliberately outside ``SourceRef``. A lineage source has to resolve to the
    complete object graph that reproduces it, and a release manifest is only the root
    of one: its dataset manifests, Parquet objects, and Raw archives are what would
    have to be enumerated, verified, and protected from retention. Admitting the kind
    into the union before the publisher, reader, retention planner, and pin all walk
    that closure would let a build claim a lineage nothing keeps whole.
    """

    kind: Literal["l1_release"]
    manifest_version: int = Field(ge=1)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        key = validate_lake_object_key(value)
        if not key.startswith("lake/manifests/releases/l1/") or not key.endswith(".json"):
            raise ValueError("l1_release must reference an L1 release manifest")
        return key

    @model_validator(mode="after")
    def validate_identity(self) -> L1ReleaseSourceRef:
        if self.key != release_manifest_key(release_id=self.source_id):
            raise ValueError("l1_release key does not match source_id")
        return self


type SourceRef = Annotated[
    RawIngestSourceRef | SQLiteSnapshotSourceRef,
    Field(discriminator="kind"),
]

type RetainedSourceRef = RawIngestSourceRef

# What an analytical cohort may be built from. Provider Raw is outside it by
# construction rather than by a check: Raw is addressed by request range and can be
# fetched again, so a cohort naming it would not be pinned to one generation of
# anything. Excluding the kind from the union is what makes that unrepresentable
# instead of merely rejected.
#
# One kind, so no cohort source is retained and `source_assurance` is `trace_only` for
# every cohort that exists. Widening this to admit an L1 release is what turns the
# assurance into a distinction — and the same change has to bring back a check that the
# sources a generation states still resolve before it becomes current, which is dead
# code while nothing retained can be named here.
type CohortSourceRef = SQLiteSnapshotSourceRef


def _source_identity(source: SourceRef) -> tuple[str, str, str]:
    """What makes two lineage references the same generation.

    The key is not part of it. A key is derived from the identity where one exists, so
    including it would let a reference without one look like a different shape of value
    rather than the same question answered with fewer fields.
    """

    return (source.kind, source.source_id, source.sha256)


def retained_sources(sources: Iterable[SourceRef]) -> tuple[RetainedSourceRef, ...]:
    """The subset whose bytes the lake stores, in the order they were declared.

    Resolution, reachability, and publication all act on exactly this subset, and each
    of them derives it here rather than by testing kinds locally, so a new source kind
    joins or stays out of all three at once.
    """

    return tuple(source for source in sources if not isinstance(source, SQLiteSnapshotSourceRef))


SourceAssurance = Literal["rebuildable_input", "trace_only"]


def source_assurance(sources: Iterable[SourceRef]) -> SourceAssurance:
    """Whether a cohort's lineage lets it be re-derived, named by its weakest source.

    **rebuildable_input** keeps the upstream data the producer read, so a cohort can be
    re-derived after a logic error is found in the producer itself. **trace_only** names
    the store generation a build read without keeping it, so it cannot.

    No cohort reaches the first value today: ``CohortSourceRef`` admits only a sealed
    SQLite snapshot, whose bytes the lake deliberately does not store. The distinction
    is derived rather than declared, so it starts describing something the moment an L1
    release becomes admissible as a cohort source — and until then the honest answer to
    "may this decide production" is no.

    The value is the weakest of the sources stated, which is also why a caller can pass
    the sources of several cohort roles at once and get the assurance of the whole.
    Stating no source at all is the weaker claim rather than the absence of a claim.
    """

    stated = tuple(sources)
    if not stated or len(retained_sources(stated)) != len(stated):
        return "trace_only"
    return "rebuildable_input"


class LakeObject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: str
    sha256: str
    bytes: int = Field(gt=0)
    rows: int = Field(ge=0)
    min_key: tuple[str, ...] = Field(min_length=1)
    max_key: tuple[str, ...] = Field(min_length=1)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        return validate_lake_object_key(value)

    @field_validator("sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        return validate_sha256(value)

    @model_validator(mode="after")
    def validate_key_range(self) -> LakeObject:
        if len(self.min_key) != len(self.max_key):
            raise ValueError("min_key and max_key must have the same arity")
        if self.min_key > self.max_key:
            raise ValueError("min_key cannot sort after max_key")
        return self


class PartitionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    values: Mapping[str, PartitionValue] = Field(min_length=1)
    objects: tuple[LakeObject, ...] = Field(min_length=1)
    sources: tuple[SourceRef, ...] = ()
    source_state_sha256: str

    @field_validator("values")
    @classmethod
    def freeze_values(cls, values: Mapping[str, PartitionValue]) -> Mapping[str, PartitionValue]:
        return MappingProxyType(dict(values))

    @field_serializer("values")
    def serialize_values(self, values: Mapping[str, PartitionValue]) -> dict[str, PartitionValue]:
        return dict(values)

    @field_validator("sources")
    @classmethod
    def validate_sources(cls, values: tuple[SourceRef, ...]) -> tuple[SourceRef, ...]:
        identities = {_source_identity(item) for item in values}
        if len(identities) != len(values):
            raise ValueError("partition sources cannot contain duplicates")
        return values

    @field_validator("source_state_sha256")
    @classmethod
    def validate_source_state_sha256(cls, value: str) -> str:
        return validate_sha256(value)


class ManifestTotals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    objects: int = Field(ge=0)
    bytes: int = Field(ge=0)
    rows: int = Field(ge=0)


class MeasurementPolicyRef(BaseModel):
    """Under which screening rules and panel contract a cohort was measured.

    The rules a cohort was screened under decide which names are in it and what each
    row's status is, so two cohorts measured under different rules are not one series
    even though their columns line up. That fact lived only inside a diagnostics row,
    which means a consumer had to read Parquet to learn it and a bundle could be
    assembled from a mixture without anything in the manifest disagreeing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    rules_hash: str
    panel_variant: str
    production_authority: bool

    @field_validator("rules_hash", "panel_variant")
    @classmethod
    def validate_identifier_field(cls, value: str) -> str:
        if not value:
            raise ValueError("measurement policy fields cannot be empty")
        return value


class CohortInventoryEntry(BaseModel):
    """Manifest-only proof that one analytical cohort was evaluated."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: CohortStatus
    rows: int = Field(ge=0)
    sources: tuple[CohortSourceRef, ...] = Field(min_length=1)
    input_cutoff: date
    measurement_policy: MeasurementPolicyRef

    @field_validator("sources")
    @classmethod
    def validate_sources(cls, values: tuple[CohortSourceRef, ...]) -> tuple[CohortSourceRef, ...]:
        identities = {_source_identity(item) for item in values}
        if len(identities) != len(values):
            raise ValueError("cohort sources cannot contain duplicates")
        return values

    @model_validator(mode="after")
    def validate_status(self) -> CohortInventoryEntry:
        if self.status == "complete" and self.rows == 0:
            raise ValueError("complete cohort must contain rows")
        if self.status in {"empty", "not_computed"} and self.rows != 0:
            raise ValueError(f"{self.status} cohort cannot contain rows")
        if any(self.input_cutoff > source.captured_at.date() for source in self.sources):
            raise ValueError("cohort input cutoff cannot follow SQLite snapshot capture")
        return self


class CalibrationDatasetRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dataset: str
    build_id: str
    manifest_key: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rows: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_identity(self) -> CalibrationDatasetRef:
        if self.dataset not in CALIBRATION_DATASETS:
            raise ValueError("bundle references an unsupported calibration dataset")
        if self.manifest_key != dataset_manifest_key(dataset=self.dataset, build_id=self.build_id):
            raise ValueError("bundle dataset key does not match its identity")
        return self


class CalibrationCohortInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    panel: CohortInventoryEntry
    diagnostics: CohortInventoryEntry
    forward: CohortInventoryEntry

    @model_validator(mode="after")
    def validate_composition(self) -> CalibrationCohortInventory:
        if self.diagnostics.status not in {"complete", "partial"}:
            raise ValueError("computed panel cohort requires diagnostics")
        if self.diagnostics.status == "complete" and self.diagnostics.rows != 1:
            raise ValueError("complete diagnostics cohort must contain exactly one row")
        if self.panel.status == "not_computed":
            raise ValueError("a bundle cannot publish an uncomputed panel cohort")
        if (
            self.panel.sources != self.diagnostics.sources
            or self.panel.input_cutoff != self.diagnostics.input_cutoff
        ):
            raise ValueError("panel and diagnostics must use the same cohort input")
        # The rules decide which names are in the cohort and what each row's status is,
        # so the outcome half and the cross-section half have to have been measured the
        # same way. Leaving this to whichever writer assembled the bundle means an
        # alternate producer can state the inconsistency in the wire format itself.
        if not (
            self.panel.measurement_policy
            == self.diagnostics.measurement_policy
            == self.forward.measurement_policy
        ):
            raise ValueError("cohort roles disagree about the rules they were measured under")
        return self


class ForwardObservationPolicyRef(BaseModel):
    """The runtime observation rules a bundle's forward cohorts were measured under."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    use_control_event_exits: bool


class CalibrationBundleManifest(BaseModel):
    """One externally visible calibration generation across all three datasets.

    ``assembled_by_git_commit`` is the transaction identity of the bundle, not the
    identity of the code that produced its datasets. Each dataset manifest keeps its
    own ``producer_git_commit``, so a bundle that matures forward outcomes on top of
    panels built earlier states both facts instead of restating one as the other.
    Compatibility between the datasets is decided by their transform fingerprints,
    cohort sources, and cutoffs — not by a shared commit.

    There is deliberately no bundle-level compatibility field. Compatibility is a
    per-dataset question and every answer lives in the dataset manifest the bundle
    already names, so a summary here would be a second statement of the same fact that
    nothing derives, verifies, or reads: an alternate writer could record any value and
    every reader would still be right. A generation says what it is by naming its three
    builds.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    manifest_version: Literal[1] = 1
    bundle_id: str
    created_at: datetime
    assembled_by_git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    # The contract a reader has to know before it can decide whether this generation is
    # the one it wants. It rides in the manifest the pointer names so that switching
    # generations is one atomic act: a separate file stating the contract could land
    # while the pointer did not, leaving a store that describes a generation it is not
    # serving and refuses to read the one it is.
    forward_observation_policy: ForwardObservationPolicyRef
    datasets: Mapping[str, CalibrationDatasetRef]

    @field_validator("bundle_id")
    @classmethod
    def validate_bundle_id(cls, value: str) -> str:
        return validate_identifier(value, label="bundle_id")

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be UTC")
        return value

    @field_validator("datasets")
    @classmethod
    def freeze_datasets(
        cls, values: Mapping[str, CalibrationDatasetRef]
    ) -> Mapping[str, CalibrationDatasetRef]:
        if set(values) != CALIBRATION_DATASETS:
            raise ValueError("bundle must reference the complete calibration dataset set")
        if any(name != item.dataset for name, item in values.items()):
            raise ValueError("bundle dataset mapping keys must match their references")
        return MappingProxyType(dict(values))

    @field_serializer("datasets")
    def serialize_datasets(
        self, values: Mapping[str, CalibrationDatasetRef]
    ) -> dict[str, CalibrationDatasetRef]:
        return dict(values)


def require_calibration_generation(
    manifests: Mapping[str, DatasetManifest],
) -> Mapping[str, CalibrationCohortInventory]:
    """Compose three dataset manifests into one generation's cohort inventory.

    Each dataset manifest already states which cohorts its build holds and what each one
    is. Deriving the bundle's view from them means the two cannot disagree, so there is
    nothing to keep in step and nothing to check in three places. What the bundle asserts
    that a dataset manifest cannot is that these three are one series, and that is what
    this refuses to compose when it is false.
    """

    if set(manifests) != CALIBRATION_DATASETS:
        raise ValueError("a calibration generation is exactly its three datasets")
    inventories = {name: manifests[name].cohort_inventory for name in CALIBRATION_DATASETS}
    asofs = set(inventories["calibration.panel"])
    for name, inventory in inventories.items():
        if set(inventory) != asofs:
            raise ValueError(f"calibration datasets publish different cohorts: {name}")
    cohorts = {
        asof: CalibrationCohortInventory(
            panel=inventories["calibration.panel"][asof],
            diagnostics=inventories["calibration.panel_diagnostics"][asof],
            forward=inventories["calibration.forward"][asof],
        )
        for asof in sorted(asofs)
    }
    require_one_generation(cohorts)
    return MappingProxyType(cohorts)


def require_one_generation(cohorts: Mapping[str, CalibrationCohortInventory]) -> None:
    """Refuse a cohort set that is not one series measured one way.

    The bundle manifest does not carry the inventory — each dataset manifest already
    states which cohorts its build holds, and a second copy is a second place for the
    same fact to be written and a third place to check that the two agree. What the
    bundle *is* is the claim that these three datasets are one generation, and that
    claim has cross-cohort content: keys are canonical as-ofs, a panel is measured as of
    its own cohort date, an outcome is observed no earlier than the cross-section it
    describes, and every cohort was screened under the same rules. Cohorts measured
    under different rules answer different questions, so aggregating them reports a
    change in the rules as a change in the market — and the aggregate is what a decision
    reads.
    """

    for asof, cohort in cohorts.items():
        if date.fromisoformat(asof).isoformat() != asof:
            raise ValueError("bundle cohort keys must be canonical ISO dates")
        cohort_asof = date.fromisoformat(asof)
        if cohort.panel.input_cutoff != cohort_asof:
            raise ValueError("panel input cutoff must equal its cohort as-of")
        if cohort.forward.input_cutoff < cohort_asof:
            raise ValueError("forward input cutoff cannot precede its cohort as-of")
    if not cohorts:
        raise ValueError("a calibration generation publishes at least one cohort")
    policies = {cohort.panel.measurement_policy for cohort in cohorts.values()}
    if len(policies) > 1:
        raise ValueError("bundle cohorts mix measurement policies")


class CalibrationBundleRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    bundle_id: str
    manifest_key: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity(self) -> CalibrationBundleRef:
        if self.manifest_key != calibration_bundle_manifest_key(bundle_id=self.bundle_id):
            raise ValueError("bundle ref key does not match its identity")
        return self


class CalibrationBundlePointer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pointer_version: Literal[1] = 1
    current: CalibrationBundleRef


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    manifest_version: Literal[1]
    dataset: str
    layer: ManifestLayer
    contract_version: int = Field(ge=1)
    build_id: str
    sources: tuple[SourceRef, ...]
    producer_git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    transform_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    created_at: datetime
    coverage_start: date
    data_as_of: date
    population_count: int | None = Field(default=None, ge=0)
    """How many distinct subjects the dataset covers, where that is a meaningful count.

    A per-issue fact table answers "how much of the market is in here", which is what a
    release policy checks before serving it. A calendar or a filing index has no such
    subject: every row is about a day or a document. Reporting zero there would claim an
    empty population instead of an inapplicable question, so the absence is `None`.
    """
    coverage_status: CoverageStatus
    partition_by: tuple[str, ...] = Field(min_length=1)
    cohort_inventory: Mapping[str, CohortInventoryEntry] = Field(default_factory=dict)
    # An analytical build can legitimately publish nothing — a forward cohort where
    # no observation has resolved yet is a real state, and representing it as an
    # absent build would make "not computed" indistinguishable from "computed and
    # empty". A canonical L1 build with no partition is not a usable release input.
    partitions: tuple[PartitionManifest, ...] = ()
    totals: ManifestTotals

    @field_validator("dataset")
    @classmethod
    def validate_dataset(cls, value: str) -> str:
        return validate_dataset_name(value)

    @field_validator("build_id")
    @classmethod
    def validate_build_id(cls, value: str) -> str:
        return validate_identifier(value, label="build_id")

    @field_validator("producer_git_commit")
    @classmethod
    def validate_producer_commit(cls, value: str) -> str:
        if value == "0" * 40:
            raise ValueError("producer_git_commit cannot be an unknown zero identity")
        return value

    @field_validator("sources")
    @classmethod
    def validate_sources(cls, values: tuple[SourceRef, ...]) -> tuple[SourceRef, ...]:
        identities = {_source_identity(item) for item in values}
        if len(identities) != len(values):
            raise ValueError("dataset sources cannot contain duplicates")
        return values

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be UTC")
        return value

    @field_validator("partition_by")
    @classmethod
    def validate_partition_by(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_partition_layout(value)

    @field_validator("cohort_inventory")
    @classmethod
    def freeze_cohort_inventory(
        cls, values: Mapping[str, CohortInventoryEntry]
    ) -> Mapping[str, CohortInventoryEntry]:
        for asof in values:
            if date.fromisoformat(asof).isoformat() != asof:
                raise ValueError("cohort inventory keys must be canonical ISO dates")
        return MappingProxyType(dict(values))

    @field_serializer("cohort_inventory")
    def serialize_cohort_inventory(
        self, values: Mapping[str, CohortInventoryEntry]
    ) -> dict[str, CohortInventoryEntry]:
        return dict(values)

    @model_validator(mode="after")
    def validate_semantics(self) -> DatasetManifest:
        if self.coverage_start > self.data_as_of:
            raise ValueError("coverage_start cannot be after data_as_of")
        if self.population_count is not None and self.population_count > self.totals.rows:
            raise ValueError("population_count cannot exceed total rows")
        if self.layer == "l1_canonical":
            if self.population_count is not None and self.population_count == 0:
                raise ValueError("l1_canonical population_count must be positive")
            if self.cohort_inventory:
                raise ValueError("l1_canonical cannot contain analytical cohort inventory")
            if self.sources:
                raise ValueError("l1_canonical keeps lineage on each partition")
            if not self.partitions:
                raise ValueError("l1_canonical requires at least one partition")
            if any(not partition.sources for partition in self.partitions):
                raise ValueError("each L1 partition requires source lineage")
            if any(
                source.kind not in {"raw_ingest", "sqlite_snapshot"}
                for partition in self.partitions
                for source in partition.sources
            ):
                raise ValueError("L1 partitions accept Raw ingest or SQLite snapshot sources only")
        else:
            if self.sources:
                raise ValueError("L2 lineage belongs to each analytical cohort")
            if not self.cohort_inventory:
                raise ValueError("l2_analytical requires computed cohort inventory")
            if any(not item.sources for item in self.cohort_inventory.values()):
                raise ValueError("each L2 cohort requires a fixed input generation source")
            if any(partition.sources for partition in self.partitions):
                raise ValueError("L2 lineage belongs to each cohort, not each partition")
        # A contract version pins the layout it was written under. Readers resolve
        # partitions by the layout the manifest declares, so a build that changed grain
        # under an unchanged version would be read with the old expectation and silently
        # mean something else. Changing the grain is a contract bump.
        contract = LAKE_DATASETS.get(self.dataset)
        if (
            contract is not None
            and self.contract_version == contract.contract_version
            and tuple(self.partition_by) != contract.partition_by
        ):
            raise ValueError("dataset contract version pins its partition layout")

        partition_identities: set[tuple[tuple[str, PartitionValue], ...]] = set()
        object_keys: set[str] = set()
        object_count = 0
        byte_count = 0
        row_count = 0
        for partition in self.partitions:
            identity = tuple(sorted(partition.values.items()))
            if identity in partition_identities:
                raise ValueError("partition values cannot be repeated")
            partition_identities.add(identity)
            for lake_object in partition.objects:
                expected_key = canonical_object_key(
                    layer=self.layer,
                    dataset=self.dataset,
                    contract_version=self.contract_version,
                    partition_values=partition.values,
                    content_sha256=lake_object.sha256,
                    partition_by=self.partition_by,
                )
                if lake_object.key != expected_key:
                    raise ValueError("object key does not match manifest identity")
                if lake_object.key in object_keys:
                    raise ValueError("object keys cannot be repeated")
                object_keys.add(lake_object.key)
                object_count += 1
                byte_count += lake_object.bytes
                row_count += lake_object.rows
        expected_totals = (object_count, byte_count, row_count)
        actual_totals = (self.totals.objects, self.totals.bytes, self.totals.rows)
        if actual_totals != expected_totals:
            raise ValueError("totals must equal the manifest object inventory")
        if self.layer == "l2_analytical":
            inventory_rows = sum(item.rows for item in self.cohort_inventory.values())
            if inventory_rows != self.totals.rows:
                raise ValueError("L2 cohort inventory rows must equal manifest totals")
        return self


class ReleaseDataset(BaseModel):
    """One dataset build a release pins, named by identity and closed by digest.

    ``manifest_sha256`` is what makes the release fix data rather than names. A
    dataset manifest key is derived from ``(dataset, build_id)``, so without the
    digest the same release could be made to resolve to different objects by
    republishing that key — and every downstream check would pass, because the
    object digests it compares against come from the replaced manifest.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    build_id: str
    contract_version: int = Field(ge=1)
    manifest_sha256: str
    data_as_of: date
    coverage_status: CoverageStatus
    totals: ManifestTotals

    @field_validator("build_id")
    @classmethod
    def validate_build_id(cls, value: str) -> str:
        return validate_identifier(value, label="build_id")

    @field_validator("manifest_sha256")
    @classmethod
    def validate_manifest_digest(cls, value: str) -> str:
        return validate_sha256(value)


class ReleaseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    manifest_version: Literal[1]
    release_id: str
    profile: ReleaseProfile
    created_at: datetime
    data_as_of: date
    datasets: Mapping[str, ReleaseDataset] = Field(min_length=1)

    @field_validator("release_id")
    @classmethod
    def validate_release_id(cls, value: str) -> str:
        return validate_identifier(value, label="release_id")

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be UTC")
        return value

    @field_validator("datasets")
    @classmethod
    def validate_datasets(
        cls, values: Mapping[str, ReleaseDataset]
    ) -> Mapping[str, ReleaseDataset]:
        for dataset in values:
            validate_dataset_name(dataset)
        return MappingProxyType(dict(values))

    @field_serializer("datasets")
    def serialize_datasets(self, values: Mapping[str, ReleaseDataset]) -> dict[str, ReleaseDataset]:
        return dict(values)

    @model_validator(mode="after")
    def validate_inventory(self) -> ReleaseManifest:
        if self.data_as_of != min(item.data_as_of for item in self.datasets.values()):
            raise ValueError("release data_as_of must equal the oldest dataset watermark")
        return self


class ReleaseDatasetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dataset: str
    required: bool
    accepted_contract_versions: tuple[int, ...] = Field(min_length=1)
    coverage_start_on_or_before: date | None
    """How far back this dataset's history must reach, or absent when it has no history.

    A dataset whose SQLite table is replaced by each fetch holds a current view rather
    than an archive: `jquants.earnings_calendar` is the forward announcement calendar, so
    its earliest row moves forward every time the exchange drops a past announcement.
    Pinning a start date against a sliding view states a promise the source never made
    and fails on a schedule — the daily batch stopped on 2026-08-17 because the snapshot
    had advanced from 2026-06-19 to 2026-07-03. Absent means the dataset carries no
    history floor; every other check (rows, population, staleness) still applies.
    """
    minimum_rows: int = Field(gt=0)
    minimum_population_count: int | None = Field(default=None, gt=0)
    """Absent for datasets whose rows have no per-subject population to count."""
    max_age_days: int = Field(ge=0)
    """How stale this dataset's watermark may be against the evaluation date.

    Cadence belongs to the dataset, not to the profile. A weekly balance published with
    a reporting lag and a daily bar are both current at watermarks two weeks apart, so
    one shared limit has to be loose enough for the slowest and stops saying anything
    about the fastest.
    """
    require_complete_coverage: bool = True
    """Whether this dataset must prove complete coverage to enter a release.

    A source with no fetch record cannot prove it, and refusing it would mean the lake
    can never carry it. Serving what it holds while saying so is the honest state; the
    flag is per dataset because completeness is a property of what records the source,
    not of the release the dataset happens to join.
    """
    max_lead_days: int = Field(default=0, ge=0)
    """How far ahead of the evaluation date this dataset legitimately publishes.

    A market calendar names business days that have not happened yet, and an earnings
    calendar names announcements that have not been made. Their watermarks are supposed
    to be in the future; treating that as staleness inverted would refuse the release.
    """

    @field_validator("dataset")
    @classmethod
    def validate_dataset(cls, value: str) -> str:
        return validate_dataset_name(value)

    @field_validator("accepted_contract_versions")
    @classmethod
    def validate_versions(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(isinstance(value, bool) or value < 1 for value in values):
            raise ValueError("accepted contract versions must be positive integers")
        if len(values) != len(set(values)):
            raise ValueError("accepted contract versions cannot contain duplicates")
        return values


class ReleasePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    policy_version: Literal[1]
    profile: ReleaseProfile
    datasets: tuple[ReleaseDatasetPolicy, ...] = Field(min_length=1)
    max_manifest_bytes: int = Field(gt=0)
    max_objects: int = Field(gt=0)
    # Whether every dataset in the release must have been exported from one and the same
    # sealed SQLite generation. That is a property of how a profile's datasets are
    # produced, not of what a release is: a dataset built from provider Raw has no
    # SQLite generation to share, and a rule stated here lets such a dataset join a
    # release under its own profile instead of requiring the constructor to change.
    require_shared_snapshot_generation: bool

    @model_validator(mode="after")
    def validate_dataset_inventory(self) -> ReleasePolicy:
        names = [item.dataset for item in self.datasets]
        if len(names) != len(set(names)):
            raise ValueError("release policy datasets cannot contain duplicates")
        if not any(item.required for item in self.datasets):
            raise ValueError("release policy requires at least one dataset")
        return self


PRODUCTION_RELEASE_POLICY = ReleasePolicy(
    policy_version=1,
    profile="production",
    datasets=(
        ReleaseDatasetPolicy(
            dataset="jquants.daily_bars",
            required=True,
            accepted_contract_versions=(1,),
            coverage_start_on_or_before=date(2016, 8, 1),
            minimum_rows=9_634_243,
            minimum_population_count=5_097,
            require_complete_coverage=True,
            max_age_days=31,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.short_sale_reports",
            required=True,
            accepted_contract_versions=(1,),
            coverage_start_on_or_before=date(2016, 8, 10),
            minimum_rows=1_342_362,
            minimum_population_count=3_907,
            require_complete_coverage=True,
            max_age_days=31,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.weekly_margin",
            required=True,
            accepted_contract_versions=(1,),
            coverage_start_on_or_before=date(2016, 8, 5),
            minimum_rows=1_960_849,
            minimum_population_count=4_866,
            require_complete_coverage=False,
            max_age_days=45,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.master_snapshots",
            required=True,
            accepted_contract_versions=(1,),
            coverage_start_on_or_before=date(2016, 9, 30),
            minimum_rows=548_341,
            minimum_population_count=5_073,
            require_complete_coverage=False,
            max_age_days=31,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.fin_summaries",
            required=True,
            accepted_contract_versions=(1,),
            coverage_start_on_or_before=date(2016, 8, 1),
            minimum_rows=173_198,
            minimum_population_count=4_425,
            require_complete_coverage=True,
            max_age_days=31,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.market_calendar",
            required=True,
            accepted_contract_versions=(1,),
            coverage_start_on_or_before=date(2016, 8, 1),
            minimum_rows=3_524,
            require_complete_coverage=True,
            max_age_days=31,
            max_lead_days=400,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.earnings_calendar",
            required=True,
            accepted_contract_versions=(1,),
            # Replaced by every snapshot fetch, so its earliest row is whatever the
            # exchange still publishes rather than a history this release keeps.
            coverage_start_on_or_before=None,
            # A forward calendar's size is seasonal, so a floor taken from one snapshot
            # bounds the season it was taken in rather than a broken fetch. Measured on
            # actual disclosures 2023-08 onward, the number of companies announcing in
            # any 68-day window (this snapshot's span) ranges 978 to 4,699 with a median
            # of 3,985; the trough is mid-November three years running. The previous
            # floor of 3,232 — one observation of 3,403 times 0.95 — sits above 32% of
            # those windows, so it refused a healthy publication every autumn. What the
            # floor is for is a fetch that returned a fraction of the calendar, so it is
            # set at half the observed minimum: low enough that no measured window
            # breaches it, high enough that a truncated response does.
            minimum_rows=489,
            minimum_population_count=489,
            require_complete_coverage=True,
            max_age_days=31,
            max_lead_days=120,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.margin_alerts",
            required=True,
            accepted_contract_versions=(1,),
            coverage_start_on_or_before=date(2026, 8, 3),
            minimum_rows=1_657,
            minimum_population_count=214,
            require_complete_coverage=True,
            max_age_days=31,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.all_issues_daily_margin",
            required=False,
            accepted_contract_versions=(1,),
            coverage_start_on_or_before=date(2026, 9, 25),
            minimum_rows=1,
            require_complete_coverage=False,
            max_age_days=31,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.documents",
            required=True,
            accepted_contract_versions=(1,),
            coverage_start_on_or_before=date(2024, 7, 31),
            minimum_rows=160_990,
            require_complete_coverage=True,
            max_age_days=31,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.metrics",
            required=True,
            accepted_contract_versions=(1,),
            coverage_start_on_or_before=date(2026, 5, 8),
            minimum_rows=131_909,
            minimum_population_count=3_788,
            require_complete_coverage=False,
            max_age_days=31,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.document_lists",
            required=True,
            accepted_contract_versions=(1,),
            coverage_start_on_or_before=date(2024, 7, 31),
            minimum_rows=706,
            require_complete_coverage=False,
            max_age_days=31,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.buyback_reports",
            required=True,
            accepted_contract_versions=(1,),
            coverage_start_on_or_before=date(2024, 6, 30),
            minimum_rows=5_871,
            minimum_population_count=1_159,
            require_complete_coverage=False,
            max_age_days=62,
        ),
        ReleaseDatasetPolicy(
            dataset="jpx.regulation_flags",
            required=True,
            accepted_contract_versions=(1,),
            coverage_start_on_or_before=date(2026, 5, 8),
            minimum_rows=4_534,
            minimum_population_count=141,
            require_complete_coverage=False,
            max_age_days=31,
        ),
        ReleaseDatasetPolicy(
            dataset="jpx.regulation_sources",
            required=True,
            accepted_contract_versions=(1,),
            coverage_start_on_or_before=date(2026, 5, 8),
            minimum_rows=133,
            require_complete_coverage=False,
            max_age_days=31,
        ),
    ),
    max_manifest_bytes=16 * 1024 * 1024,
    max_objects=10_000,
    require_shared_snapshot_generation=True,
)


def release_policy_for_profile(profile: ReleaseProfile) -> ReleasePolicy:
    """Return the registered release policy; an unconfigured profile fails closed.

    There is one profile because there is one set of requirements. The gates that
    matter are per dataset — the row floor that catches a lossy export, the coverage
    boundary, the cadence-aware freshness window — and they do not become different
    requirements because a release is read by a different caller. A second profile
    carrying the same fifteen entries would be two tables free to rot apart, so the
    manifest records which gate it passed and there is exactly one gate to pass.
    """

    if profile == "production":
        return PRODUCTION_RELEASE_POLICY
    raise ValueError(f"release policy is not configured for profile: {profile}")


type Manifest = DatasetManifest | ReleaseManifest


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _load_json_object(raw: str | bytes) -> dict[str, object]:
    wire_bytes = raw if isinstance(raw, bytes) else raw.encode()
    if len(wire_bytes) > MAX_LAKE_JSON_BYTES:
        raise ValueError("lake JSON payload exceeds the wire size limit")
    try:
        payload = json.loads(wire_bytes, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("invalid JSON payload") from None
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")
    return payload


def _validate_payload[LakeModelT: BaseModel](
    payload: dict[str, object], model: type[LakeModelT]
) -> LakeModelT:
    try:
        canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return model.model_validate_json(canonical)
    except ValidationError as exc:
        error = exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )[0]
        location = ".".join(str(item) for item in error["loc"]) or "root"
        raise ValueError(f"invalid {model.__name__}: {error['type']} at {location}") from None


def load_lake_model_json[LakeModelT: BaseModel](
    raw: str | bytes, model: type[LakeModelT]
) -> LakeModelT:
    """Parse one lake wire model with duplicate-key rejection and redacted errors."""
    return _validate_payload(_load_json_object(raw), model)


def load_manifest_json(raw: str | bytes) -> Manifest:
    """Parse one strict manifest without accepting ambiguous or duplicate fields."""
    payload = _load_json_object(raw)
    is_dataset = "dataset" in payload
    is_release = "release_id" in payload
    if is_dataset == is_release:
        raise ValueError("manifest must identify exactly one dataset build or L1 release")
    if is_dataset:
        return _validate_payload(payload, DatasetManifest)
    return _validate_payload(payload, ReleaseManifest)


def canonical_lake_model_bytes(model: BaseModel) -> bytes:
    """Serialize one validated lake model for immutable storage and digesting."""
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def validate_release_policy(
    release: ReleaseManifest,
    manifests: Mapping[str, DatasetManifest],
    *,
    evaluated_at: datetime,
) -> None:
    """Require a release to be complete and fresh for its declared profile."""
    if evaluated_at.utcoffset() != timedelta(0):
        raise ValueError("release evaluation time must be UTC")
    if release.created_at > evaluated_at:
        raise ValueError("release creation time cannot be after its evaluation time")
    policy = release_policy_for_profile(release.profile)
    policy_by_dataset = {item.dataset: item for item in policy.datasets}
    unknown = set(release.datasets) - set(policy_by_dataset)
    if unknown:
        raise ValueError("release contains a dataset outside its profile")
    required = {item.dataset for item in policy.datasets if item.required}
    if not required.issubset(release.datasets):
        raise ValueError("release is missing a required dataset")
    if set(manifests) != set(release.datasets):
        raise ValueError("release validation requires every referenced dataset manifest")
    # Structural first: whether these datasets are one picture of the store at all comes
    # before whether that picture is fresh enough to publish.
    if policy.require_shared_snapshot_generation:
        _require_shared_snapshot_generation(manifests.values())

    total_manifest_bytes = len(canonical_lake_model_bytes(release))
    total_objects = 0
    for dataset, release_dataset in release.datasets.items():
        manifest = manifests[dataset]
        dataset_policy = policy_by_dataset[dataset]
        if manifest.layer != "l1_canonical" or manifest.dataset != dataset:
            raise ValueError(f"{dataset}: release accepts matching L1 dataset manifests only")
        if manifest.created_at > release.created_at:
            raise ValueError(f"{dataset}: release cannot predate a referenced dataset manifest")
        if manifest.contract_version not in dataset_policy.accepted_contract_versions:
            raise ValueError(
                f"{dataset}: contract v{manifest.contract_version} is not accepted by its "
                f"profile {dataset_policy.accepted_contract_versions}"
            )
        manifest_bytes = canonical_lake_model_bytes(manifest)
        if release_dataset.manifest_sha256 != sha256(manifest_bytes).hexdigest():
            raise ValueError(f"{dataset}: release dataset manifest digest does not match")
        if (
            release_dataset.build_id != manifest.build_id
            or release_dataset.contract_version != manifest.contract_version
            or release_dataset.data_as_of != manifest.data_as_of
            or release_dataset.coverage_status != manifest.coverage_status
            or release_dataset.totals != manifest.totals
        ):
            raise ValueError(f"{dataset}: release dataset inventory does not match its manifest")
        if dataset_policy.require_complete_coverage and manifest.coverage_status != "complete":
            raise ValueError(
                f"{dataset}: coverage is {manifest.coverage_status}, but its profile requires "
                "complete"
            )
        if (
            dataset_policy.coverage_start_on_or_before is not None
            and manifest.coverage_start > dataset_policy.coverage_start_on_or_before
        ):
            raise ValueError(
                f"{dataset}: coverage starts {manifest.coverage_start}, later than the profile "
                f"boundary {dataset_policy.coverage_start_on_or_before}"
            )
        if manifest.totals.rows < dataset_policy.minimum_rows:
            raise ValueError(
                f"{dataset}: {manifest.totals.rows} row(s) is below the profile floor "
                f"{dataset_policy.minimum_rows}"
            )
        if dataset_policy.minimum_population_count is not None:
            if manifest.population_count is None:
                raise ValueError(f"{dataset}: does not report the population it is floored on")
            if manifest.population_count < dataset_policy.minimum_population_count:
                raise ValueError(
                    f"{dataset}: population {manifest.population_count} is below the profile "
                    f"floor {dataset_policy.minimum_population_count}"
                )
        age = (evaluated_at.date() - manifest.data_as_of).days
        if age > dataset_policy.max_age_days or -age > dataset_policy.max_lead_days:
            raise ValueError("release dataset is outside its freshness window")
        total_manifest_bytes += len(manifest_bytes)
        total_objects += manifest.totals.objects

    # No separate skew limit: every watermark was just measured against its own dataset's
    # window around the same evaluation date, so a dataset that stopped updating is
    # already refused by its own bound. Comparing watermarks to each other instead would
    # ask a daily bar and a monthly filing index to agree on a date they never share.
    if total_manifest_bytes > policy.max_manifest_bytes:
        raise ValueError("release manifest graph exceeds the profile byte budget")
    if total_objects > policy.max_objects:
        raise ValueError("release object inventory exceeds the profile budget")


def _require_shared_snapshot_generation(manifests: Iterable[DatasetManifest]) -> None:
    """Every dataset was exported from one sealed read of the legacy store.

    Two datasets exported from different seals describe the store at two different
    moments, and a release presents them as one consistent picture of it.
    """

    identities: set[tuple[str, str, int]] = set()
    for manifest in manifests:
        manifest_identities = {
            (source.source_id, source.sha256, source.schema_version)
            for partition in manifest.partitions
            for source in partition.sources
            if isinstance(source, SQLiteSnapshotSourceRef)
        }
        if len(manifest_identities) != 1:
            raise ValueError(
                f"{manifest.dataset} must reference exactly one SQLite snapshot generation"
            )
        identities.update(manifest_identities)
    if len(identities) != 1:
        raise ValueError("L1 release datasets must share one SQLite snapshot generation")
