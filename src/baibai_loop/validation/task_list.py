"""Validate the canonical operational task list."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from baibai_loop.foundation.errors import ValidationFinding
from baibai_loop.foundation.yaml_io import safe_load

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "records" / "_schemas" / "task-list.json"
TASK_LIST_FILENAME = "tasks.yaml"


def _load_validator() -> Draft202012Validator:
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected schema root: {SCHEMA_PATH}")
    Draft202012Validator.check_schema(raw)
    return Draft202012Validator(raw, format_checker=FormatChecker())


_VALIDATOR = _load_validator()


def discover_task_list_files(root: Path) -> list[Path]:
    path = root / TASK_LIST_FILENAME
    return [path] if path.is_file() else []


def validate_task_list_file(path: Path) -> list[ValidationFinding]:
    raw = _load_yaml(path)
    if isinstance(raw, list):
        return raw
    findings = _validate_schema(path, raw)
    if findings:
        return findings
    tasks = raw["tasks"]
    assert isinstance(tasks, list)
    identifiers = [task["task_id"] for task in tasks if isinstance(task, Mapping)]
    duplicates = sorted(
        identifier for identifier in set(identifiers) if identifiers.count(identifier) > 1
    )
    return [
        _finding(
            path,
            "task-list.duplicate-task-id",
            f"task_id must be unique: {identifier}",
            "tasks",
        )
        for identifier in duplicates
    ]


def _load_yaml(path: Path) -> Mapping[str, object] | list[ValidationFinding]:
    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        return [_finding(path, "task-list.io", f"failed to read task list: {error}")]
    except yaml.YAMLError as error:
        return [_finding(path, "task-list.invalid-yaml", f"YAML parse failed: {error}")]
    if not isinstance(raw, Mapping):
        return [_finding(path, "task-list.non-mapping", "task list root must be a mapping")]
    return raw


def _validate_schema(path: Path, document: Mapping[str, object]) -> list[ValidationFinding]:
    return [
        _finding(
            path,
            f"task-list.{error.validator or 'invalid'}",
            error.message,
            _format_path(error.absolute_path),
        )
        for error in _VALIDATOR.iter_errors(document)
    ]


def _finding(
    path: Path,
    code: str,
    message: str,
    location: str | None = None,
) -> ValidationFinding:
    return ValidationFinding("error", path, code, message, location)


def _format_path(parts: Iterable[Any]) -> str:
    rendered: list[str] = []
    for part in parts:
        rendered.append(
            f"[{part}]" if isinstance(part, int) else f".{part}" if rendered else str(part)
        )
    return "".join(rendered)
