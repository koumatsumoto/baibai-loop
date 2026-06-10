"""Shared helpers for domain-model validators."""

from __future__ import annotations

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


def repository_ref_error(ref: object, *, root: Path | None = None) -> str | None:
    """Return a reason when a repository reference is not safe to resolve."""
    if not isinstance(ref, str) or not ref:
        return "reference must be a non-empty repository-relative path"
    ref_path = Path(ref)
    if ref_path.is_absolute():
        return "reference must not be an absolute path"
    if any(part == ".." for part in ref_path.parts):
        return "reference must not contain '..'"
    if root is not None:
        root_resolved = root.resolve()
        target_resolved = (root / ref_path).resolve(strict=False)
        if target_resolved != root_resolved and root_resolved not in target_resolved.parents:
            return "reference must resolve inside the repository root"
    return None


def resolve_repository_ref(root: Path, ref: str) -> Path:
    """Resolve a safe repository-relative reference."""
    error = repository_ref_error(ref, root=root)
    if error is not None:
        raise ValueError(error)
    return root / ref


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


def load_reference_mapping(root: Path, reference: object) -> tuple[Path, Mapping[str, Any]]:
    if not isinstance(reference, Mapping):
        raise ValueError("reference must be a mapping")
    ref = reference.get("ref_path")
    error = repository_ref_error(ref, root=root)
    if error is not None:
        raise ValueError(error)
    assert isinstance(ref, str)
    path = resolve_repository_ref(root, ref)
    if path.suffix == ".md":
        payload = load_markdown_front_matter(path)
    else:
        raw = load_yaml_file(path)
        if not isinstance(raw, Mapping):
            raise ValueError(f"{path}: reference payload must be a mapping")
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
