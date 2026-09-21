"""L1 market dataset・releaseの不整合を止めるimmutable manifest model。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from hashlib import sha256
from types import MappingProxyType
from typing import Annotated, Literal

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
    canonical_object_key,
    release_manifest_key,
    validate_dataset_name,
    validate_identifier,
    validate_lake_object_key,
    validate_partition_layout,
    validate_sha256,
)

ManifestLayer = Literal["l1_canonical"]
CoverageStatus = Literal["complete", "partial"]
ReleaseProfile = Literal["production"]
MAX_LAKE_JSON_BYTES = 16 * 1024 * 1024


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


class SQLiteSnapshotSourceRef(_SourceRefBase):
    """L1 exportが読んだ一時SQLite snapshotのschema・digest・取得時刻を示す。

    snapshotは一回のbuild中の読取世代を固定する。sourceは保持objectのkeyを持たず、
    build完了後のsnapshot bytes取得や、全入力の再構築を保証しない。
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


class L1ReleaseSourceRef(_SourceRefBase):
    """固定releaseのquery / hydrateへID・manifest key・digestを渡す読取参照。"""

    key: str
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


# Build sources identify temporary sealed SQLite inputs; their bytes are not retained.
type SourceRef = Annotated[SQLiteSnapshotSourceRef, Field(discriminator="kind")]


def _source_identity(source: SourceRef) -> tuple[str, str, str]:
    """入力snapshotが同じ世代かを識別する。"""

    return (source.kind, source.source_id, source.sha256)


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


class ManifestTotals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    objects: int = Field(ge=0)
    bytes: int = Field(ge=0)
    rows: int = Field(ge=0)


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    manifest_version: Literal[1]
    dataset: str
    layer: ManifestLayer
    contract_version: int = Field(ge=1)
    build_id: str
    sources: tuple[SourceRef, ...]
    producer_git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
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
    partitions: tuple[PartitionManifest, ...] = Field(min_length=1)
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

    @model_validator(mode="after")
    def validate_semantics(self) -> DatasetManifest:
        if self.coverage_start > self.data_as_of:
            raise ValueError("coverage_start cannot be after data_as_of")
        if self.population_count is not None and self.population_count > self.totals.rows:
            raise ValueError("population_count cannot exceed total rows")
        if self.population_count is not None and self.population_count == 0:
            raise ValueError("l1_canonical population_count must be positive")
        if self.sources:
            raise ValueError("l1_canonical keeps lineage on each partition")
        if any(not partition.sources for partition in self.partitions):
            raise ValueError("each L1 partition requires source lineage")
        if any(
            source.kind != "sqlite_snapshot"
            for partition in self.partitions
            for source in partition.sources
        ):
            raise ValueError("L1 partitions accept SQLite snapshot sources only")
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
    carries_history: bool
    """Whether this dataset accumulates an archive, so its start must never move forward.

    A dataset whose SQLite table is replaced by each fetch holds a current view rather
    than an archive: `jpx.earnings_calendar` is the forward announcement calendar, so
    its earliest row moves forward every time the exchange drops a past announcement.
    False exempts it; every other check (rows, population) still applies.

    For the rest the floor is the release already serving, not a date written here. A
    pinned date states the value it had the day it was written, and measured on
    2026-08-25 every one of them sat at exactly zero days of slack — the first day any
    archive started later, the batch stopped. That is how `jpx.earnings_calendar`
    stopped it on 2026-08-17, from 2026-06-19 to 2026-07-03. Comparing against what was
    published says the thing actually meant — this release must not drop history the last
    one served — and needs no maintenance to keep saying it.
    """
    minimum_rows: int = Field(gt=0)
    minimum_population_count: int | None = Field(default=None, gt=0)
    """Absent for datasets whose rows have no per-subject population to count."""
    require_complete_coverage: bool = True
    """Whether this dataset must prove complete coverage to enter a release.

    A source with no fetch record cannot prove it, and refusing it would mean the lake
    can never carry it. Serving what it holds while saying so is the honest state; the
    flag is per dataset because completeness is a property of what records the source,
    not of the release the dataset happens to join.
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
    # produced, not of what a release is: a dataset produced without reading a store has
    # no SQLite generation to share, and a rule stated here lets such a dataset join a
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
            carries_history=True,
            minimum_rows=9_634_243,
            minimum_population_count=5_097,
            require_complete_coverage=True,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.short_sale_reports",
            required=True,
            accepted_contract_versions=(1,),
            carries_history=True,
            minimum_rows=1_342_362,
            minimum_population_count=3_907,
            require_complete_coverage=True,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.weekly_margin",
            required=True,
            accepted_contract_versions=(1,),
            carries_history=True,
            minimum_rows=1_960_849,
            minimum_population_count=4_866,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.master_snapshots",
            required=True,
            accepted_contract_versions=(1,),
            carries_history=True,
            minimum_rows=548_341,
            minimum_population_count=5_073,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.fin_summaries",
            required=True,
            accepted_contract_versions=(1,),
            carries_history=True,
            minimum_rows=173_198,
            minimum_population_count=4_425,
            require_complete_coverage=True,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.market_calendar",
            required=True,
            accepted_contract_versions=(1,),
            carries_history=True,
            minimum_rows=3_524,
            require_complete_coverage=True,
        ),
        ReleaseDatasetPolicy(
            dataset="jpx.earnings_calendar",
            required=True,
            accepted_contract_versions=(1,),
            # Replaced by every snapshot fetch, so its earliest row is whatever the
            # exchange still publishes rather than a history this release keeps.
            carries_history=False,
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
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.margin_alerts",
            required=True,
            accepted_contract_versions=(1,),
            carries_history=True,
            minimum_rows=1_657,
            minimum_population_count=214,
            require_complete_coverage=True,
        ),
        ReleaseDatasetPolicy(
            dataset="jquants.all_issues_daily_margin",
            required=False,
            accepted_contract_versions=(1,),
            carries_history=True,
            minimum_rows=1,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.documents",
            required=True,
            accepted_contract_versions=(1,),
            carries_history=True,
            minimum_rows=160_990,
            require_complete_coverage=True,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.segment_facts",
            required=False,
            accepted_contract_versions=(1,),
            carries_history=True,
            minimum_rows=1,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.debt_schedule",
            required=False,
            accepted_contract_versions=(1,),
            carries_history=True,
            minimum_rows=1,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.metrics",
            required=True,
            accepted_contract_versions=(2,),
            carries_history=True,
            minimum_rows=131_909,
            minimum_population_count=3_788,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.document_lists",
            required=True,
            accepted_contract_versions=(1,),
            carries_history=True,
            minimum_rows=706,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="jpx.regulation_flags",
            required=True,
            accepted_contract_versions=(1,),
            carries_history=True,
            minimum_rows=4_534,
            minimum_population_count=141,
            require_complete_coverage=False,
        ),
        ReleaseDatasetPolicy(
            dataset="jpx.delistings",
            required=True,
            accepted_contract_versions=(1,),
            # Accumulating by construction: the store keeps a delisting that JPX's
            # archive page has since dropped, so its earliest row does not move.
            carries_history=True,
            # Half the 760 rows held on 2026-08-19. A derivation that returned a
            # fraction of the archive breaches it; normal growth never approaches it.
            minimum_rows=380,
            minimum_population_count=380,
            # No fetch record: the operator derives this rather than a provider serving
            # it, so nothing can prove the archive was read completely.
            require_complete_coverage=False,
            # The watermark is the latest delisting date, and JPX schedules them ahead —
            # 15 of the 760 rows were still in the future on 2026-08-19, the furthest by
            # 135 days. This dataset is refreshed by an operator command rather than by
            # the daily batch, so the freshness window is an abandonment detector, not a
            # cadence: firing it would stop the whole daily run over a monthly chore,
            # and stale exits are caught where they matter by the cohort that reads them.
        ),
        ReleaseDatasetPolicy(
            dataset="edinet.tender_offer_exit_values",
            required=True,
            accepted_contract_versions=(1,),
            # Replaced wholesale by each derivation, so a reclassified offer moves the
            # earliest row. Pinning a start date would refuse exactly the correction the
            # derivation exists to make.
            carries_history=False,
            # Half the 152 rows held on 2026-08-19.
            minimum_rows=76,
            minimum_population_count=76,
            require_complete_coverage=False,
            # Operator-run like the delistings above; see that entry for why the window
            # is wide. This watermark is always past — an exit is a completed event.
        ),
        ReleaseDatasetPolicy(
            dataset="jpx.regulation_sources",
            required=True,
            accepted_contract_versions=(1,),
            carries_history=True,
            minimum_rows=133,
            require_complete_coverage=False,
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
    carrying the same seventeen entries would be two tables free to rot apart, so the
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
    published_coverage_start: Mapping[str, date] | None = None,
) -> None:
    """Require a release to be structurally complete for its declared profile.

    No freshness is checked here, at write or at read. How old a dataset's watermark is
    changes nothing about whether the release describes its objects truthfully, and the
    readers that care (a stale weekly balance, say) null that axis themselves. A bound
    here would refuse a current release the day a source paused, and stop every hydrate
    after it.

    ``published_coverage_start`` is what the serving release covers, per dataset. It is
    the floor an archival dataset must still reach. Absent — a first publication, or a
    dataset this release introduces — leaves the history check with nothing to compare
    and the remaining checks unchanged.
    """
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
    # Whether these datasets are one picture of the store at all comes before what the
    # picture says.
    if policy.require_shared_snapshot_generation:
        _require_shared_snapshot_generation(manifests.values())

    total_manifest_bytes = len(canonical_lake_model_bytes(release))
    total_objects = 0
    for dataset, release_dataset in release.datasets.items():
        manifest = manifests[dataset]
        dataset_policy = policy_by_dataset[dataset]
        if manifest.dataset != dataset:
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
        served_start = (published_coverage_start or {}).get(dataset)
        if (
            dataset_policy.carries_history
            and served_start is not None
            and manifest.coverage_start > served_start
        ):
            raise ValueError(
                f"{dataset}: coverage starts {manifest.coverage_start}, later than the "
                f"{served_start} the serving release already covers; restore the history "
                "in the store, or set carries_history=False if this source stopped "
                "accumulating"
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
        total_manifest_bytes += len(manifest_bytes)
        total_objects += manifest.totals.objects

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
        }
        if len(manifest_identities) != 1:
            raise ValueError(
                f"{manifest.dataset} must reference exactly one SQLite snapshot generation"
            )
        identities.update(manifest_identities)
    if len(identities) != 1:
        raise ValueError("L1 release datasets must share one SQLite snapshot generation")
