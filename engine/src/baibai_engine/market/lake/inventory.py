"""Read-only inventory of a local mirror of the R2 lake namespace."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from .keys import validate_lake_object_key
from .models import RawArchiveMetadata, load_lake_model_json

RAW_SOFT_BUDGET_BYTES = {
    "preserve": 500 * 1024**3,
    "buffer": 50 * 1024**3,
}


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
                "soft_budget_bytes": RAW_SOFT_BUDGET_BYTES[name],
                "budget_exceeded": raw_totals[name][1] > RAW_SOFT_BUDGET_BYTES[name],
            }
            for name in ("preserve", "buffer")
        ],
        "raw_inventory_errors": raw_inventory_errors,
        "raw_unclassified": {
            "objects": len(orphan_payloads),
            "bytes": sum(raw_payloads[key] for key in orphan_payloads),
        },
        "invalid_keys": invalid_keys,
    }
