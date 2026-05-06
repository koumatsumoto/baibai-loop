"""Shared helpers for domain-model validators."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def repo_root_for(path: Path) -> Path:
    """Return the repository root for a record or source file path."""
    resolved = path.resolve()
    for parent in (resolved.parent, *resolved.parents):
        if (parent / "records").is_dir() and (parent / "src").is_dir():
            return parent
    cwd = Path.cwd()
    if (cwd / "records").is_dir() and (cwd / "src").is_dir():
        return cwd
    return resolved.parent


def resolve_ref(root: Path, ref: str) -> Path:
    """Resolve a repository-relative reference."""
    ref_path = Path(ref)
    if ref_path.is_absolute():
        return ref_path
    return root / ref_path


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_yaml_file(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_markdown_front_matter(path: Path) -> dict[str, Any]:
    match = _FRONT_MATTER_RE.match(path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"{path}: markdown file has no YAML front matter")
    raw = yaml.safe_load(match.group(1))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: YAML front matter must be a mapping")
    return raw


def load_snapshot_mapping(root: Path, snapshot_ref: object) -> tuple[Path, Mapping[str, Any]]:
    if not isinstance(snapshot_ref, Mapping):
        raise ValueError("snapshot reference must be a mapping")
    ref = (
        snapshot_ref.get("ref_path") or snapshot_ref.get("ref") or snapshot_ref.get("snapshot_path")
    )
    if not isinstance(ref, str) or not ref:
        raise ValueError("snapshot reference must include ref_path/ref/snapshot_path")
    path = resolve_ref(root, ref)
    if path.suffix == ".md":
        payload = load_markdown_front_matter(path)
    else:
        raw = load_yaml_file(path)
        if not isinstance(raw, Mapping):
            raise ValueError(f"{path}: snapshot payload must be a mapping")
        payload = dict(raw)
    return path, payload


def as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def integer(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
