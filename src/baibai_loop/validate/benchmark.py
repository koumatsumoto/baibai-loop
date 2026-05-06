"""Benchmark manifest validation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from .errors import ValidationFinding

_VALID_LAYERS = {"L1", "L2", "L3a", "L3b", "L4", "L5"}


def discover_benchmark_manifest_files(root: Path) -> list[Path]:
    """Return domain model benchmark manifests."""
    if not root.exists():
        return []
    return sorted(root.glob("*/manifest.yaml"))


def validate_benchmark_manifest_file(path: Path) -> list[ValidationFinding]:
    """Validate benchmark manifest structure and fixture bindings."""
    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="benchmark.parse",
                message=f"failed to read benchmark manifest: {exc}",
            )
        ]
    if not isinstance(raw, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="benchmark.root",
                message="benchmark manifest must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    findings.extend(_check_required(path, raw))
    findings.extend(_check_layers(path, raw))
    findings.extend(_check_fixtures(path, raw))
    findings.extend(_check_business_invariants(path, raw))
    return findings


def _check_required(path: Path, manifest: Mapping[str, object]) -> list[ValidationFinding]:
    required = ("benchmark_id", "manifest_version", "input_snapshots", "fixtures")
    return [
        _finding(path, "benchmark.required", f"missing required manifest field: {field}", field)
        for field in required
        if field not in manifest
    ]


def _check_layers(path: Path, manifest: Mapping[str, object]) -> list[ValidationFinding]:
    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers:
        return [_finding(path, "benchmark.layers", "layers must be a non-empty list", "layers")]
    findings: list[ValidationFinding] = []
    seen: set[str] = set()
    for index, layer in enumerate(layers):
        if not isinstance(layer, Mapping):
            findings.append(
                _finding(path, "benchmark.layer", "layer must be a mapping", f"layers[{index}]")
            )
            continue
        layer_id = layer.get("layer_id")
        if not isinstance(layer_id, str) or layer_id not in _VALID_LAYERS:
            findings.append(
                _finding(
                    path, "benchmark.layer-id", "invalid layer_id", f"layers[{index}].layer_id"
                )
            )
            continue
        if layer_id in seen:
            findings.append(
                _finding(
                    path, "benchmark.layer-duplicate", "duplicate layer_id", f"layers[{index}]"
                )
            )
        seen.add(layer_id)
    return findings


def _check_fixtures(path: Path, manifest: Mapping[str, object]) -> list[ValidationFinding]:
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        return [
            _finding(path, "benchmark.fixtures", "fixtures must be a non-empty list", "fixtures")
        ]
    findings: list[ValidationFinding] = []
    seen: set[str] = set()
    for index, fixture in enumerate(fixtures):
        if not isinstance(fixture, Mapping):
            findings.append(
                _finding(
                    path, "benchmark.fixture", "fixture must be a mapping", f"fixtures[{index}]"
                )
            )
            continue
        fixture_id = fixture.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id:
            findings.append(
                _finding(
                    path,
                    "benchmark.fixture-id",
                    "fixture_id must be a non-empty string",
                    f"fixtures[{index}].fixture_id",
                )
            )
        elif fixture_id in seen:
            findings.append(
                _finding(
                    path,
                    "benchmark.fixture-duplicate",
                    "fixture_id must be unique",
                    f"fixtures[{index}].fixture_id",
                )
            )
        else:
            seen.add(fixture_id)
        if fixture.get("layer_id") not in _VALID_LAYERS:
            findings.append(
                _finding(
                    path,
                    "benchmark.fixture-layer",
                    "fixture layer_id must reference a known benchmark layer",
                    f"fixtures[{index}].layer_id",
                )
            )
        if not isinstance(fixture.get("fixture_binding"), Mapping):
            findings.append(
                _finding(
                    path,
                    "benchmark.fixture-binding",
                    "fixture_binding must be a mapping",
                    f"fixtures[{index}].fixture_binding",
                )
            )
    return findings


def _check_business_invariants(
    path: Path, manifest: Mapping[str, object]
) -> list[ValidationFinding]:
    invariants = manifest.get("business_invariants")
    if not isinstance(invariants, list) or not invariants:
        return [
            _finding(
                path,
                "benchmark.business-invariants",
                "business_invariants must be a non-empty list",
                "business_invariants",
            )
        ]
    return []


def _finding(path: Path, code: str, message: str, location: str) -> ValidationFinding:
    return ValidationFinding(
        severity="error",
        target=path,
        code=code,
        message=message,
        location=location,
    )
