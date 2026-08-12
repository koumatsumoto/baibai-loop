"""Read-only inventory of a local mirror of the R2 lake namespace."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from pydantic import JsonValue

from .keys import validate_lake_object_key


def _area(key: str) -> str:
    parts = key.split("/")
    if parts[:3] == ["lake", "l1", "raw"] and len(parts) >= 5:
        return "/".join(parts[:5])
    if parts[:3] == ["lake", "l1", "canonical"] and len(parts) >= 4:
        return "/".join(parts[:4])
    if parts[:2] == ["lake", "l2"] and len(parts) >= 3:
        return "/".join(parts[:3])
    if parts[:2] == ["lake", "manifests"] and len(parts) >= 3:
        return "/".join(parts[:3])
    if parts[:2] == ["lake", "pointers"] and len(parts) >= 3:
        return "/".join(parts[:3])
    return "/".join(parts[:3])


def inventory(root: Path) -> dict[str, JsonValue]:
    """Count objects and bytes without reading or modifying object contents."""
    if not root.exists():
        return {
            "schema_version": 1,
            "kind": "lake_inventory",
            "root": str(root),
            "exists": False,
            "objects": 0,
            "bytes": 0,
            "areas": [],
            "invalid_keys": [],
        }
    if not root.is_dir():
        raise ValueError("inventory root must be a directory")

    totals: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
    invalid_keys: list[JsonValue] = []
    objects = 0
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        key = path.relative_to(root).as_posix()
        size = path.stat().st_size
        objects += 1
        total_bytes += size
        try:
            validate_lake_object_key(key)
        except ValueError:
            invalid_keys.append(key)
            continue
        area = totals[_area(key)]
        area[0] += 1
        area[1] += size

    areas: list[JsonValue] = [
        {"prefix": prefix, "objects": values[0], "bytes": values[1]}
        for prefix, values in sorted(totals.items())
    ]
    return {
        "schema_version": 1,
        "kind": "lake_inventory",
        "root": str(root),
        "exists": True,
        "objects": objects,
        "bytes": total_bytes,
        "areas": areas,
        "invalid_keys": invalid_keys,
    }
