"""Calendar snapshot validation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .errors import ValidationFinding


def discover_calendar_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.glob("**/*.yaml") if path.is_file())


def validate_calendar_file(path: Path) -> list[ValidationFinding]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="calendar.parse",
                message=f"failed to read calendar snapshot: {exc}",
            )
        ]
    if not isinstance(raw, Mapping):
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="calendar.root",
                message="calendar snapshot must be a mapping",
            )
        ]
    findings: list[ValidationFinding] = []
    for field in ("covered_from", "covered_until", "last_refreshed_at", "source_status"):
        if field not in raw:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="calendar.required",
                    message=f"missing calendar field: {field}",
                    location=field,
                )
            )
    if "corporate-actions" in path.parts:
        findings.extend(_check_corporate_action_metrics(path, raw))
    return findings


def _check_corporate_action_metrics(
    path: Path, raw: Mapping[object, object]
) -> list[ValidationFinding]:
    rules = _load_metric_event_invalidation_rules(path)
    if not rules:
        return []
    findings: list[ValidationFinding] = []
    events = raw.get("events")
    if not isinstance(events, list):
        return findings
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            continue
        kind = event.get("corporate_action_kind") or event.get("event_kind")
        if not isinstance(kind, str) or not kind:
            continue
        disallowed = sorted(
            metric
            for metric in _string_list(event.get("invalidates_metrics"))
            if kind not in rules.get(metric, set())
        )
        if disallowed:
            findings.append(
                ValidationFinding(
                    severity="error",
                    target=path,
                    code="calendar.corporate-action-catalog-mismatch",
                    message=(
                        "invalidates_metrics contains metrics not allowed by metric catalog "
                        f"for {kind}: {', '.join(disallowed)}"
                    ),
                    location=f"events[{index}].invalidates_metrics",
                )
            )
    return findings


def _load_metric_event_invalidation_rules(path: Path) -> dict[str, set[str]]:
    root = _repo_root_for(path)
    catalog_root = root / "records/_config/metric-catalog"
    if not catalog_root.is_dir():
        return {}
    rules: dict[str, set[str]] = {}
    for catalog_path in sorted(catalog_root.glob("*.yaml")):
        try:
            catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(catalog, Mapping):
            continue
        for metric in _as_list(catalog.get("metrics")):
            if not isinstance(metric, Mapping):
                continue
            metric_id = metric.get("metric_id")
            if not isinstance(metric_id, str) or not metric_id:
                continue
            metric_rules = rules.setdefault(metric_id, set())
            for rule in _as_list(metric.get("event_invalidation_rules")):
                if not isinstance(rule, Mapping):
                    continue
                kind = rule.get("corporate_action_kind") or rule.get("kind")
                if isinstance(kind, str) and kind:
                    metric_rules.add(kind)
    return rules


def _repo_root_for(path: Path) -> Path:
    for parent in (path.resolve().parent, *path.resolve().parents):
        if (parent / "records").exists():
            return parent
    return path.resolve().parent


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _string_list(value: object) -> list[str]:
    return [str(item) for item in _as_list(value) if isinstance(item, str)]
