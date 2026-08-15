"""Read-only inventory of a local mirror of the R2 lake namespace."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from .keys import validate_lake_object_key
from .models import RawArchiveMetadata, load_lake_model_json

# One capacity policy, split by what each class is for, so a class that grows for a
# design reason is visible against the objective that class was sized against. A single
# figure cannot do that: provider originals that cannot be re-fetched need a budget two
# orders of magnitude above the published graph, and reporting only the larger one lets
# the published graph grow past its objective without anything saying so.
SOFT_BUDGET_BYTES = {
    # The published lake: canonical Parquet, analytical Parquet, manifests, pointers.
    # This is the class Issue #917 sized at roughly 10 GB.
    "published": 10 * 1024**3,
    # Provider originals that cannot be retrieved again. Sized separately and approved
    # separately; this is not the published-graph objective under another name.
    "raw_preserve": 500 * 1024**3,
    # Provider responses a re-fetch can reproduce.
    "raw_buffer": 50 * 1024**3,
    # In-flight staging and the quarantine a failed build was moved to. Neither is under
    # any manifest, so nothing else in this report grows when they do — and both hold
    # whole sealed stores, so a repeated large failure is otherwise an invisible leak.
    "workspace": 20 * 1024**3,
}


def _capacity_class(key: str) -> str | None:
    """The budget a stored key counts against, or ``None`` when another class holds it."""
    if key.startswith("lake/l1/raw/"):
        return None  # Counted by retention class from its metadata sidecar.
    if key.startswith(("lake/staging/", "lake/quarantine/")):
        return "workspace"
    if key.startswith(("lake/l1/", "lake/l2/", "lake/manifests/", "lake/pointers/")):
        return "published"
    return None


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
    """Count object metadata; only Raw JSON sidecars are parsed for retention class."""
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
            "raw_retention": [],
            "raw_inventory_errors": [],
            "raw_unclassified": {"objects": 0, "bytes": 0},
            "invalid_keys": [],
        }
    if not root.is_dir():
        raise ValueError("inventory root must be a directory")

    totals: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
    invalid_keys: list[JsonValue] = []
    objects = 0
    total_bytes = 0
    raw_totals: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
    raw_oldest: dict[str, datetime] = {}
    raw_inventory_errors: list[JsonValue] = []
    raw_payloads: dict[str, int] = {}
    raw_metadata_objects: set[str] = set()
    capacity: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
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
        capacity_class = _capacity_class(key)
        if capacity_class is not None:
            counter = capacity[capacity_class]
            counter[0] += 1
            counter[1] += size
        if key.startswith("lake/l1/raw/") and key.endswith(".metadata.json"):
            try:
                metadata = load_lake_model_json(path.read_bytes(), RawArchiveMetadata)
                archived = root / metadata.object_key
                if not archived.is_file() or archived.stat().st_size != metadata.bytes:
                    raise ValueError("Raw object is missing or has a different size")
            except (OSError, ValueError) as exc:
                raw_inventory_errors.append({"key": key, "error": str(exc)})
                continue
            raw_metadata_objects.add(metadata.object_key)
            retention = raw_totals[metadata.retention_class]
            retention[0] += 1
            retention[1] += metadata.bytes
            raw_oldest[metadata.retention_class] = min(
                metadata.retrieved_at,
                raw_oldest.get(metadata.retention_class, metadata.retrieved_at),
            )
        elif key.startswith("lake/l1/raw/") and key.endswith((".json.gz", ".csv.gz", ".zip")):
            raw_payloads[key] = size

    orphan_payloads = sorted(set(raw_payloads) - raw_metadata_objects)
    raw_inventory_errors.extend(
        {"key": key, "error": "Raw object has no metadata sidecar"} for key in orphan_payloads
    )

    counted = {
        name: (raw_totals[name.removeprefix("raw_")] if name.startswith("raw_") else capacity[name])
        for name in SOFT_BUDGET_BYTES
    }
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
        "raw_retention": [
            {
                "class": name,
                "objects": raw_totals[name][0],
                "bytes": raw_totals[name][1],
                "oldest_retrieved_at": (
                    raw_oldest[name].astimezone(UTC).isoformat() if name in raw_oldest else None
                ),
            }
            for name in ("preserve", "buffer")
        ],
        "capacity": [
            {
                "class": name,
                "objects": counted[name][0],
                "bytes": counted[name][1],
                "soft_budget_bytes": budget,
                "budget_exceeded": counted[name][1] > budget,
            }
            for name, budget in sorted(SOFT_BUDGET_BYTES.items())
        ],
        "raw_inventory_errors": raw_inventory_errors,
        "raw_unclassified": {
            "objects": len(orphan_payloads),
            "bytes": sum(raw_payloads[key] for key in orphan_payloads),
        },
        "invalid_keys": invalid_keys,
    }
