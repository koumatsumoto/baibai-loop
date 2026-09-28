"""workspaceのファイル読取・書込と共通エラー。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

import yaml

from baibai_engine.foundation.filesystem import write_text_atomic
from baibai_engine.foundation.yaml_io import safe_load


class ResearchWorkspaceError(Exception):
    """Base error carrying the CLI exit code for the failure class."""

    exit_code = 3


class ResearchWorkspaceDataError(ResearchWorkspaceError):
    """Missing source / schema / hash makes the request unprocessable."""

    exit_code = 3


class ResearchWorkspaceConflictError(ResearchWorkspaceError):
    """Output collision, input hash drift, or path-confinement violation."""

    exit_code = 4


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dump_yaml(payload: object) -> str:
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _write_workspace_file(path: Path, payload: object) -> str:
    text = _dump_yaml(payload)
    write_text_atomic(path, text)
    return _sha256_text(text)


def _load_mapping(path: Path, *, label: str) -> dict[str, object]:
    if not path.exists():
        raise ResearchWorkspaceDataError(f"{label} not found: {path}")
    raw = safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ResearchWorkspaceDataError(f"{label} root must be a mapping: {path}")
    return dict(raw)


def _nonempty_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchWorkspaceDataError(f"{label} must be a non-empty string")
    return value


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_date(value: str, *, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ResearchWorkspaceDataError(f"invalid {label}: {value}") from error
