"""Read-only inventory of a local mirror of the R2 lake namespace."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from pydantic import JsonValue

from .keys import validate_lake_object_key

# One capacity policy, split by what each class is for, so a class that grows for a
# design reason is visible against the objective that class was sized against. A single
# figure cannot do that: provider originals that cannot be re-fetched need a budget two
# orders of magnitude above the published graph, and reporting only the larger one lets
# the published graph grow past its objective without anything saying so.
SOFT_BUDGET_BYTES = {
    # The published lake: canonical Parquet, analytical Parquet, manifests, pointers.
    # This is the class Issue #917 sized at roughly 10 GB.
    "published": 10 * 1024**3,
    # In-flight staging, including what a failed build left behind. It is under no
    # manifest, so nothing else in this report grows when it does — and it holds whole
    # sealed stores, so a repeated large failure is otherwise an invisible leak.
    "workspace": 20 * 1024**3,
}


# Files the operation writes beside the lake namespace, not objects the lake publishes.
_CONTROL_FILES = frozenset({".lake-writer.lock"})

# The mirror root is a directory the operator also keeps stores and generation records in,
# so the lake is one subtree of it rather than the whole thing.
_NAMESPACE = "lake"


def _capacity_class(key: str) -> str | None:
    """The budget a stored key counts against, or ``None`` when another class holds it."""
    if key.startswith("lake/staging/"):
        return "workspace"
    if key.startswith(("lake/l1/", "lake/manifests/", "lake/pointers/")):
        return "published"
    return None


def _area(key: str) -> str:
    parts = key.split("/")
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
    invalid_keys: list[JsonValue] = []
    objects = 0
    total_bytes = 0
    capacity: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
    control_files = 0
    control_bytes = 0
    # Walking the whole root would describe the operator's directory rather than the lake:
    # a market store, a backup and a generation record are not badly named lake objects,
    # and counting them as such buries the one thing an invalid key is meant to surface.
    walked = list((root / _NAMESPACE).rglob("*"))
    walked.extend(root / name for name in _CONTROL_FILES)
    for path in sorted(walked):
        if not path.is_file():
            continue
        key = path.relative_to(root).as_posix()
        size = path.stat().st_size
        # Operational control files live beside the namespace rather than in it. Counting
        # them as lake objects makes the totals disagree with the prefix breakdown, and
        # reporting them as invalid keys makes a healthy store look corrupt.
        if key in _CONTROL_FILES:
            control_files += 1
            control_bytes += size
            continue
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
        "invalid_keys": invalid_keys,
    }
