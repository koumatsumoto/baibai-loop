"""Small immutable native dependencies, committed by an existing ready handoff."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from baibai_batch.storage.external_assessment_staging import put_private, read_private
from baibai_batch.storage.lake_publish import LakePublishError, ObjectStore


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
        references.append({
            name: value[name] for name in ("request_id", "source", "params", "received_at")
        } | {"key": key, "sha256": digest})
    return references


def available_dependencies(store: ObjectStore) -> list[dict[str, Any]]:
    # Listing is needed only for new plans whose exact master is not yet published.
    iterator = getattr(store, "iter_objects", None)
    if iterator is None:
        return []
    result = []
    for source in ("jquants-market", "tradingview"):
        for key, _ in iterator(f"coordination/market-sources/{source}-"):
            if not key.endswith("/handoff.json"):
                continue
            saved = read_private(store, key)
            if saved is None:
                continue
            try:
                manifest = json.loads(saved.payload)
                if manifest["result"]["source"] != source:
                    raise ValueError("dependency source differs from ready handoff")
                for item in manifest.get("dependencies", []):
                    if item["key"] != key.removesuffix("handoff.json") + f"dependency-{item['sha256']}.json":
                        raise ValueError("dependency object differs from ready scope")
                    datetime.fromisoformat(item["received_at"])
                    result.append(item | {"handoff_sha256": hashlib.sha256(saved.payload).hexdigest()})
            except (ValueError, KeyError, TypeError):
                # A malformed handoff is not an eligible dependency. Integration
                # independently reports it; it cannot supply a fabricated master.
                continue
    return result


def latest_dependency(
    references: list[dict[str, Any]], *, source: str, day: date,
) -> dict[str, Any] | None:
    selected = []
    for item in references:
        if item["source"] != source:
            continue
        params = item["params"]
        if ((source == "master" and params == {"date": day.isoformat()})
                or (source == "market_calendar" and set(params) == {"from", "to"}
                    and params["from"] <= day.isoformat() <= params["to"])):
            selected.append(item)
    return max(selected, key=lambda item: (datetime.fromisoformat(item["received_at"]), item["request_id"]), default=None)


def load_dependency(store: ObjectStore, reference: dict[str, Any]) -> dict[str, Any]:
    saved = read_private(store, reference["key"])
    if saved is None or hashlib.sha256(saved.payload).hexdigest() != reference["sha256"]:
        raise ValueError("fixed acquisition dependency content changed")
    value = json.loads(saved.payload)
    if any(value[name] != reference[name] for name in ("request_id", "source", "params", "received_at")):
        raise ValueError("dependency header differs from its ready receipt")
    if not isinstance(value, dict):
        raise ValueError("invalid native acquisition dependency")
    return value
