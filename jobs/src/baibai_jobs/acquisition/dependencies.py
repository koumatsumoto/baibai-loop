"""Small immutable native dependencies, committed by an existing ready handoff."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from baibai_engine.storage.objects.json import put_private, read_private
from baibai_engine.storage.objects.r2 import (
    LakePublishError,
    ObjectStore,
)


def save_dependencies(store: ObjectStore, prefix: str, directory: Path) -> list[dict[str, Any]]:
    references = []
    for path in sorted(directory.glob("dependency-*.json")):
        payload = path.read_bytes()
        value = json.loads(payload)
        digest = hashlib.sha256(payload).hexdigest()
        key = f"{prefix}/dependency-{digest}.json"
        previous = read_private(store, key)
        if previous is None:
            put_private(store, key, payload, previous_etag=None)
        elif previous.payload != payload:
            raise LakePublishError("source dependency identity changed")
        references.append(
            {name: value[name] for name in ("request_id", "source", "params", "received_at")}
            | {"key": key, "sha256": digest}
        )
    return references


def load_dependency(store: ObjectStore, reference: dict[str, Any]) -> dict[str, Any]:
    saved = read_private(store, reference["key"])
    if saved is None or hashlib.sha256(saved.payload).hexdigest() != reference["sha256"]:
        raise ValueError("fixed acquisition dependency content changed")
    value = json.loads(saved.payload)
    if any(
        value[name] != reference[name] for name in ("request_id", "source", "params", "received_at")
    ):
        raise ValueError("dependency header differs from its ready receipt")
    if not isinstance(value, dict):
        raise ValueError("invalid native acquisition dependency")
    return value
