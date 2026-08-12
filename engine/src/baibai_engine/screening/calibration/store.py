"""The calibration cohort store: immutable typed L2 builds under a moving pointer.

A cohort is written by publishing a new immutable build and advancing the dataset
pointer to it. Objects are content addressed, so a cohort that did not change costs
nothing to carry into the next build, and a build that was superseded remains
addressable until retention decides otherwise.

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

import subprocess  # nosec B404
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import cast

import yaml

from baibai_engine.foundation.repository_layout import CALIBRATION_DIR
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market.lake.models import DatasetManifest, PartitionManifest
from baibai_engine.market.lake.objects import sha256_bytes
from baibai_engine.market.lake.retention import (
    LakeRetentionError,
    advance_l2_pointer,
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
    CalibrationLakeError,
    L2BuildInputs,
    L2Dataset,
    asof_month,
    build_identifier,
    load_manifest,
    publish_l2_build,
    read_l2_partition,
    require_build_inputs,
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
    cache_meta_path(root).write_text(
        yaml.safe_dump({"cache_schema_version": CACHE_SCHEMA_VERSION}, sort_keys=False),
        encoding="utf-8",
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


def producer_git_commit() -> str:
    try:
        result = subprocess.run(  # nosec B603
            ("git", "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "0" * 40
    commit = result.stdout.strip()
    return commit if len(commit) == 40 else "0" * 40


def _inputs() -> L2BuildInputs:
    """What this build declares, and nothing it does not have.

    No L1 release is bound while calibration's inputs are still read from the legacy
    store, so the recorded release is the named value that says so rather than an id
    that would imply a provenance the build did not have.
    """

    return L2BuildInputs(
        source_release_id=LEGACY_ONLY_RELEASE_ID,
        producer_git_commit=producer_git_commit(),
        cache_schema_version=CACHE_SCHEMA_VERSION,
    )


def _require_partition_objects(
    root: Path, dataset: L2Dataset, partition: PartitionManifest
) -> None:
    """A carried partition must still have its objects, or the build inherits a hole."""

    for item in partition.objects:
        if not (root / item.key).is_file():
            raise CalibrationLakeError(f"{dataset.name}: published object is missing: {item.key}")


def _current_manifest(root: Path, dataset: L2Dataset) -> DatasetManifest | None:
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


def _publish_cohort(
    root: Path,
    *,
    dataset: L2Dataset,
    asof: date,
    rows: Sequence[object],
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
    if manifest is not None:
        # Carrying a partition from a build made under other measurement rules would
        # publish it under this build's fingerprint, which is exactly the mixing the
        # cohort contract exists to prevent. The version stamp is written only after
        # this passes, so a refused build leaves the store describing itself truthfully.
        require_build_inputs(manifest, dataset=dataset, cache_schema_version=CACHE_SCHEMA_VERSION)
    _write_cache_meta(root)
    inputs = _inputs()

    carried: list[PartitionManifest] = []
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

    fingerprint = transform_fingerprint(dataset, cache_schema_version=CACHE_SCHEMA_VERSION)
    now = datetime.now(UTC)
    report = publish_l2_build(
        dataset=dataset,
        mirror_root=root,
        partitions=carried,
        inputs=inputs,
        build_id=build_identifier(dataset=dataset, fingerprint=fingerprint, now=now),
        data_as_of=data_as_of,
        created_at=now,
    )
    advance_l2_pointer(
        root,
        dataset=dataset.name,
        build_id=report.build_id,
        manifest_path=report.manifest_path,
        expected_current_build_id=None if manifest is None else manifest.build_id,
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
) -> None:
    with _store_errors():
        _publish_cohort(root, dataset=CALIBRATION_PANEL, asof=asof, rows=rows)
        _publish_cohort(root, dataset=CALIBRATION_DIAGNOSTICS, asof=asof, rows=(diagnostics,))


def write_forward(
    root: Path,
    asof: date,
    rows: list[ForwardReturnRow],
) -> None:
    with _store_errors():
        _publish_cohort(root, dataset=CALIBRATION_FORWARD, asof=asof, rows=rows)


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
        return bool(_cohort_payloads(root, CALIBRATION_PANEL, asof))
    except (CalibrationLakeError, LakeRetentionError) as exc:
        raise CalibrationCacheError(str(exc)) from exc


def published_cohorts(root: Path) -> list[date]:
    """Every as-of the current panel build holds, in order.

    The build manifest is the inventory: a cohort exists because the current build
    publishes rows for it, not because a file with a matching name is on disk.
    """

    try:
        manifest = _current_manifest(root, CALIBRATION_PANEL)
        if manifest is None:
            return []
        asofs: set[date] = set()
        for partition in manifest.partitions:
            month = (int(partition.values["year"]), int(partition.values["month"]))
            for payload in read_l2_partition(
                dataset=CALIBRATION_PANEL,
                manifest=manifest,
                mirror_root=root,
                month=month,
                columns=["asof"],
            ):
                asofs.add(date.fromisoformat(str(payload["asof"])))
    except (CalibrationLakeError, LakeRetentionError) as exc:
        # An unreadable build is not an empty store. Reporting it as "no cohorts"
        # would make evaluate say the store holds nothing when it holds 81 cohorts.
        raise CalibrationCacheError(f"calibration cache is invalid: {exc}") from exc
    return sorted(asofs)


def _cohort_payloads(root: Path, dataset: L2Dataset, asof: date) -> list[Mapping[str, object]]:
    manifest = _current_manifest(root, dataset)
    if manifest is None:
        return []
    require_build_inputs(manifest, dataset=dataset, cache_schema_version=CACHE_SCHEMA_VERSION)
    payloads = read_l2_partition(
        dataset=dataset, manifest=manifest, mirror_root=root, month=asof_month(asof.isoformat())
    )
    return [item for item in payloads if str(item["asof"]) == asof.isoformat()]


def read_panel(root: Path, asof: date) -> list[PanelRow]:
    _require_current_cache(root)
    try:
        payloads = _cohort_payloads(root, CALIBRATION_PANEL, asof)
    except (CalibrationLakeError, LakeRetentionError) as exc:
        raise CalibrationCacheError(f"calibration panel cache is invalid: {exc}") from exc
    if not payloads:
        raise CalibrationCacheError("calibration cache is partial; run calibration-build --force")
    try:
        return [panel_row_from_mapping(payload) for payload in payloads]
    except (KeyError, TypeError, ValueError) as exc:
        raise CalibrationCacheError(
            "calibration panel cache is invalid; run calibration-build --force"
        ) from exc


def read_panel_meta(root: Path, asof: date) -> dict[str, object]:
    _require_current_cache(root)
    try:
        payloads = _cohort_payloads(root, CALIBRATION_DIAGNOSTICS, asof)
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


def read_forward(root: Path, asof: date) -> list[ForwardReturnRow]:
    _require_current_cache(root)
    try:
        manifest = _current_manifest(root, CALIBRATION_FORWARD)
        if manifest is None:
            raise CalibrationCacheError(
                "calibration cache is partial; run calibration-build --force"
            )
        require_build_inputs(
            manifest, dataset=CALIBRATION_FORWARD, cache_schema_version=CACHE_SCHEMA_VERSION
        )
        if asof > manifest.data_as_of:
            # The build has not reached this cohort. Returning an empty list would
            # send an uncomputed cohort into evaluation as "observed, nothing
            # resolved", which is the one reading the rows cannot support.
            raise CalibrationCacheError(
                "calibration cache is partial; run calibration-build --force"
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
