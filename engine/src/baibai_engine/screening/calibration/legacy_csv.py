"""Read and archive calibration inputs stored as per-cohort CSV files.

A compatible CSV store remains readable and can be migrated without recomputing its
cohorts. Migration archives every exact input byte under a typed source manifest,
checks row parity, and exposes compatible data through one L2 calibration bundle.
An incompatible store is archived for reproducibility but is not published as L2.

Values arrive as text and are coerced to the row's declared types, then put through
the same invariant checks a published build is put through. A legacy file whose
values do not satisfy the contract is refused rather than read into a cohort.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, get_args, get_type_hints

import yaml

from baibai_engine.foundation.filesystem import write_bytes_atomic
from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.market.lake.identity import verified_git_commit
from baibai_engine.market.lake.immutable import install_immutable_file
from baibai_engine.market.lake.models import (
    CalibrationInputFile,
    CalibrationInputManifest,
    CalibrationInputSourceRef,
)
from baibai_engine.market.lake.retention import lake_writer_lock
from baibai_engine.market.lake.sources import sha256_file

from .forward import ForwardReturnRow
from .lake import canonical_manifest_bytes
from .panel import PanelDiagnostics, PanelRow
from .store import (
    CACHE_SCHEMA_VERSION,
    CalibrationCacheError,
    adopt_bundle_generation,
    current_bundle_ref,
    forward_row_from_mapping,
    panel_row_from_mapping,
    read_forward,
    read_panel,
    read_panel_meta,
    resolve_calibration_bundle,
    write_forward,
    write_panel,
)


def legacy_panel_path(root: Path, asof: date) -> Path:
    return root / f"panel-{asof.isoformat()}.csv"


def legacy_panel_meta_path(root: Path, asof: date) -> Path:
    return root / f"panel-{asof.isoformat()}.meta.yaml"


def legacy_forward_path(root: Path, asof: date) -> Path:
    return root / f"forward-{asof.isoformat()}.csv"


def legacy_cohorts(root: Path) -> list[date]:
    """Every as-of a legacy cache holds, in order."""

    cohorts: list[date] = []
    for path in sorted(root.glob("panel-*.csv")):
        try:
            cohorts.append(date.fromisoformat(path.stem.removeprefix("panel-")))
        except ValueError as exc:
            raise CalibrationCacheError(f"legacy panel filename has no as-of: {path.name}") from exc
    return cohorts


def read_legacy_panel(root: Path, asof: date) -> list[PanelRow]:
    rows = _read_rows(legacy_panel_path(root, asof), PanelRow)
    return [panel_row_from_mapping(_typed(raw, PanelRow)) for raw in rows]


def read_legacy_forward(root: Path, asof: date) -> list[ForwardReturnRow]:
    rows = _read_rows(legacy_forward_path(root, asof), ForwardReturnRow)
    return [forward_row_from_mapping(_typed(raw, ForwardReturnRow)) for raw in rows]


def read_legacy_panel_meta(root: Path, asof: date) -> dict[str, Any]:
    path = legacy_panel_meta_path(root, asof)
    try:
        payload = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CalibrationCacheError(f"legacy panel metadata is unreadable: {path.name}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("rules_hash"), str):
        raise CalibrationCacheError(f"legacy panel metadata is invalid: {path.name}")
    return dict(payload)


def migrate_legacy_calibration(
    legacy_root: Path,
    destination: Path,
    *,
    report_path: Path,
) -> dict[str, object]:
    """Archive exact legacy bytes and publish compatible cohorts through one bundle cutover."""

    cohorts = legacy_cohorts(legacy_root)
    if not cohorts:
        raise CalibrationCacheError("legacy calibration store contains no cohorts")
    expected_names = {
        name
        for asof in cohorts
        for name in (
            legacy_panel_path(legacy_root, asof).name,
            legacy_panel_meta_path(legacy_root, asof).name,
            legacy_forward_path(legacy_root, asof).name,
        )
    }
    expected_names.add("calibration.meta.yaml")
    actual_names = {
        path.name
        for pattern in ("panel-*.csv", "panel-*.meta.yaml", "forward-*.csv")
        for path in legacy_root.glob(pattern)
    }
    if (legacy_root / "calibration.meta.yaml").exists():
        actual_names.add("calibration.meta.yaml")
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise CalibrationCacheError(
            f"legacy cohort inventory is partial (missing={missing}, extra={extra})"
        )
    files = [legacy_root / name for name in sorted(expected_names)]
    for path in files:
        _require_contained_regular_file(legacy_root, path)
    file_identities = {path.name: (sha256_file(path), path.stat().st_size) for path in files}
    aggregate = hashlib.sha256(
        json.dumps(file_identities, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    input_id = f"legacy-csv-{aggregate[:24]}"
    try:
        migration_commit = verified_git_commit()
    except (OSError, RuntimeError) as exc:
        raise CalibrationCacheError(str(exc)) from exc

    with lake_writer_lock(destination):
        current = current_bundle_ref(destination)
        if current is not None:
            fixed = resolve_calibration_bundle(destination)
            source_ids = {
                source.source_id
                for manifest in fixed.datasets.values()
                for cohort in manifest.cohort_inventory.values()
                for source in cohort.sources
            }
            if source_ids != {input_id}:
                raise CalibrationCacheError(
                    "legacy migration destination already has a different current bundle"
                )
            report: dict[str, object] = {
                "kind": "calibration-legacy-migration",
                "status": "already_migrated",
                "completion": "committed",
                "input_id": input_id,
                "assembled_by_git_commit": fixed.manifest.assembled_by_git_commit,
                "cohorts": [value.isoformat() for value in cohorts],
                "files": {
                    name: {"sha256": digest, "bytes": size}
                    for name, (digest, size) in file_identities.items()
                },
            }
            try:
                write_bytes_atomic(
                    report_path,
                    json.dumps(report, sort_keys=True, separators=(",", ":")).encode() + b"\n",
                )
            except OSError as exc:
                report["completion"] = "committed_with_warnings"
                report["warnings"] = [f"report write failed: {exc}"]
            return report
        work = destination.with_name(f".{destination.name}.migration.{uuid.uuid4().hex}")
        archived: dict[str, CalibrationInputFile] = {}
        for source in files:
            digest, size = file_identities[source.name]
            key = f"lake/l2/calibration-legacy/{input_id}/{source.name}"
            target = work / key
            _install_archived_copy(target, source, expected_sha256=digest)
            archived[source.name] = CalibrationInputFile(key=key, sha256=digest, bytes=size)
        input_manifest = CalibrationInputManifest(
            manifest_version=1,
            input_id=input_id,
            input_type="legacy_csv_archive",
            files=archived,
        )
        input_key = f"lake/manifests/calibration-inputs/{input_id}.json"
        input_path = work / input_key
        write_bytes_atomic(input_path, canonical_manifest_bytes(input_manifest))
        source_ref = CalibrationInputSourceRef(
            kind="calibration_input",
            source_id=input_id,
            key=input_key,
            sha256=sha256_file(input_path),
            input_type="legacy_csv_archive",
            manifest_version=1,
        )

        archive_root = work / f"lake/l2/calibration-legacy/{input_id}"
        meta_path = archive_root / "calibration.meta.yaml"
        try:
            meta = safe_load(meta_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise CalibrationCacheError("legacy cache schema metadata is unreadable") from exc
        compatible = (
            isinstance(meta, dict) and meta.get("cache_schema_version") == CACHE_SCHEMA_VERSION
        )
        parity: dict[str, bool] = {}
        rules_hashes: set[str] = set()
        if compatible:
            for asof in cohorts:
                panel = read_legacy_panel(archive_root, asof)
                forward = read_legacy_forward(archive_root, asof)
                raw_meta = read_legacy_panel_meta(archive_root, asof)
                try:
                    diagnostics = PanelDiagnostics(**raw_meta)
                except (TypeError, ValueError) as exc:
                    raise CalibrationCacheError(
                        f"legacy diagnostics contract differs: {asof.isoformat()}"
                    ) from exc
                rules_hashes.add(diagnostics.rules_hash)
                write_panel(
                    work,
                    asof,
                    tuple(panel),
                    diagnostics,
                    source=source_ref,
                    input_cutoff=asof,
                    producer_commit=migration_commit,
                )
                observed_dates = [
                    date.fromisoformat(row.exit_date)
                    for row in forward
                    if row.exit_date is not None
                ]
                write_forward(
                    work,
                    asof,
                    forward,
                    source=source_ref,
                    input_cutoff=max(observed_dates, default=asof),
                    producer_commit=migration_commit,
                )
                parity[asof.isoformat()] = (
                    read_panel(work, asof) == panel
                    and read_panel_meta(work, asof) == raw_meta
                    and read_forward(work, asof) == forward
                )
            if len(rules_hashes) != 1:
                raise CalibrationCacheError("legacy calibration store mixes rules provenance")
            if not all(parity.values()):
                raise CalibrationCacheError("legacy calibration migration parity differs")
            adopt_bundle_generation(destination, work, expected_current=None)
            status = "migrated"
        else:
            for source in sorted((work / "lake").rglob("*")):
                if source.is_file():
                    install_immutable_file(
                        destination / source.relative_to(work),
                        source,
                        expected_sha256=sha256_file(source),
                    )
            status = "archived_incompatible"
        report = {
            "kind": "calibration-legacy-migration",
            "status": status,
            "completion": "committed",
            "input_id": input_id,
            "cache_schema_version": meta.get("cache_schema_version")
            if isinstance(meta, dict)
            else None,
            "target_cache_schema_version": CACHE_SCHEMA_VERSION,
            "producer_git_commit": migration_commit,
            "cohorts": [value.isoformat() for value in cohorts],
            "files": {
                name: {"sha256": digest, "bytes": size}
                for name, (digest, size) in file_identities.items()
            },
            "parity": parity,
        }
        warnings: list[str] = []
        try:
            write_bytes_atomic(
                report_path,
                json.dumps(report, sort_keys=True, separators=(",", ":")).encode() + b"\n",
            )
        except OSError as exc:
            warnings.append(f"report write failed: {exc}")
        try:
            shutil.rmtree(work)
        except OSError as exc:
            warnings.append(f"work cleanup failed: {exc}")
        if warnings:
            report["completion"] = "committed_with_warnings"
            report["warnings"] = warnings
        return report


def _install_archived_copy(target: Path, source: Path, *, expected_sha256: str) -> None:
    """Install bytes on an inode that no mutable legacy path can modify later."""

    target.parent.mkdir(parents=True, exist_ok=True)
    captured = target.with_name(f".{target.name}.{uuid.uuid4().hex}.capture")
    try:
        shutil.copyfile(source, captured)
        with captured.open("rb") as handle:
            os.fsync(handle.fileno())
        install_immutable_file(target, captured, expected_sha256=expected_sha256)
    finally:
        captured.unlink(missing_ok=True)


def _require_contained_regular_file(root: Path, path: Path) -> None:
    resolved_root = root.resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(resolved_root):
        raise CalibrationCacheError(f"legacy input escapes its root: {path.name}")
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise CalibrationCacheError(f"legacy input is unreadable: {path.name}") from exc
    if not stat.S_ISREG(mode):
        raise CalibrationCacheError(f"legacy input is not a regular file: {path.name}")


def _read_rows(path: Path, row_type: type) -> list[dict[str, str]]:
    if not path.is_file():
        raise CalibrationCacheError(f"legacy cohort file is absent: {path.name}")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        expected = set(get_type_hints(row_type))
        missing = sorted(expected.difference(reader.fieldnames or ()))
        if missing:
            raise CalibrationCacheError(
                f"legacy cohort file is missing {', '.join(missing)}: {path.name}"
            )
        return [dict(raw) for raw in reader]


def _typed(raw: Mapping[str, str], row_type: type) -> dict[str, Any]:
    """Read one text row as the declared types, dropping columns the contract dropped."""

    hints = get_type_hints(row_type)
    return {name: _value(raw.get(name), annotation) for name, annotation in hints.items()}


def _value(text: str | None, annotation: Any) -> Any:
    members = [item for item in get_args(annotation) if item is not type(None)]
    optional = type(None) in get_args(annotation)
    target = members[0] if members else annotation
    if text is None:
        return None
    if text == "":
        # An empty cell is absence only where the field can be absent. A required
        # text field that holds "" is carrying a real value — the empty list of
        # threshold blocks reads as "measured, none fired", not as "not measured".
        return None if optional else ""
    if target is bool:
        if text not in {"true", "false"}:
            raise CalibrationCacheError(f"legacy boolean is not true or false: {text!r}")
        return text == "true"
    if target is float:
        return float(text)
    if target is int:
        return int(text)
    return text
