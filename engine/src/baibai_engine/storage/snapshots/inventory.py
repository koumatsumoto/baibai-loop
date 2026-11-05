"""Read-only inventory of a local mirror of the R2 lake namespace."""

from __future__ import annotations

from collections import defaultdict
from itertools import chain
from pathlib import Path

from pydantic import JsonValue

from baibai_engine.storage.snapshots.keys import validate_lake_object_key

# 中断したbuildの作業容量で公開objectの増加を隠さないよう、予算を分ける。
SOFT_BUDGET_BYTES = {
    "published": 10 * 1024**3,
    "workspace": 20 * 1024**3,
}

# mirror直下のstoreやbackupは集計外。lake外の制御fileもobjectと区別する。
_CONTROL_FILES = frozenset({".lake-writer.lock"})


def _capacity_class(key: str) -> str | None:
    """The budget a stored key counts against, or ``None`` when another class holds it."""
    if key.startswith("lake/staging/"):
        return "workspace"
    if key.startswith(
        ("lake/l1/source/", "lake/l2/processed/", "lake/manifests/", "lake/pointers/")
    ):
        return "published"
    return None


def _area(key: str) -> str:
    parts = key.split("/")
    if parts[:3] in (["lake", "l1", "source"], ["lake", "l2", "processed"]) and len(parts) >= 4:
        return "/".join(parts[:4])
    return "/".join(parts[:3])


def inventory(root: Path) -> dict[str, JsonValue]:
    """Count object metadata by prefix and capacity class; no object body is read."""
    if not root.exists():
        return {
            "schema_version": 1,
            "kind": "lake_inventory",
            "root": str(root),
            "exists": False,
            "objects": 0,
            "bytes": 0,
            "areas": [],
            "capacity": [],
            "control_files": {"objects": 0, "bytes": 0},
            "invalid_keys": [],
        }
    if not root.is_dir():
        raise ValueError("inventory root must be a directory")

    totals: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
    invalid_paths: list[Path] = []
    objects = 0
    total_bytes = 0
    capacity: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
    control_files = 0
    control_bytes = 0
    for path in chain((root / "lake").rglob("*"), (root / name for name in _CONTROL_FILES)):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        key = relative.as_posix()
        size = path.stat().st_size
        if key in _CONTROL_FILES:
            control_files += 1
            control_bytes += size
            continue
        objects += 1
        total_bytes += size
        try:
            validate_lake_object_key(key)
        except ValueError:
            invalid_paths.append(relative)
            continue
        area = totals[_area(key)]
        area[0] += 1
        area[1] += size
        capacity_class = _capacity_class(key)
        if capacity_class is not None:
            counter = capacity[capacity_class]
            counter[0] += 1
            counter[1] += size
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
        "capacity": [
            {
                "class": name,
                "objects": capacity[name][0],
                "bytes": capacity[name][1],
                "soft_budget_bytes": budget,
                "budget_exceeded": capacity[name][1] > budget,
            }
            for name, budget in sorted(SOFT_BUDGET_BYTES.items())
        ],
        "control_files": {"objects": control_files, "bytes": control_bytes},
        "invalid_keys": [path.as_posix() for path in sorted(invalid_paths)],
    }
