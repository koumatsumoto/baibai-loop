"""Freeze available private handoffs at integration start; no provider completion barrier."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from baibai_batch.storage.economic_staging import load_handoff, stage_prefix
from baibai_batch.storage.external_assessment_staging import put_private, read_private
from baibai_batch.storage.lake_publish import LakePublishError, ObjectStore


class ReceiptStore(ObjectStore, Protocol):
    def iter_objects(self, prefix: str) -> Iterator[tuple[str, int]]: ...


def available_handoffs(
    store: ReceiptStore, *, source: str | None = None, include_integrated: bool = False
) -> list[dict[str, object]]:
    """Freeze exact ready manifests that exist now, ignoring integrated attempts."""
    ready: list[dict[str, object]] = []
    for market in (False, True):
        namespace = "market-sources" if market else "economic-sources"
        if source is not None and source.startswith("economic-") == market:
            continue
        selection = source.removeprefix("economic-") + "-" if source is not None else ""
        for key, _ in store.iter_objects(f"coordination/{namespace}/{selection}"):
            if not key.endswith("/handoff.json"):
                continue
            stage_id = key.split("/")[-2]
            terminal = read_private(store, stage_prefix(stage_id, market=market) + "/integrated.json")
            if not include_integrated and terminal:
                continue
            saved = read_private(store, key)
            if saved is None:
                raise LakePublishError("listed source handoff is unavailable")
            manifest = json.loads(saved.payload)
            report = manifest["result"]
            if source is not None and (report["source"] if market else "economic-" + report["group"]) != source:
                raise LakePublishError("source handoff prefix contains another source")
            ready.append({
                "market": market, "stage_id": stage_id,
                "manifest_sha256": hashlib.sha256(saved.payload).hexdigest(),
                "source": report["source"] if market else "economic-" + report["group"],
                "started_at": report["started_at"], "asof": report["asof"],
                "integration": json.loads(terminal.payload) if terminal else None,
            })
    return sorted(ready, key=lambda entry: (str(entry["source"]), str(entry["started_at"]), str(entry["stage_id"])))


def download_fixed(store: ObjectStore, entry: dict[str, object], directory: Path) -> None:
    stage_id, market = str(entry["stage_id"]), bool(entry["market"])
    saved = read_private(store, stage_prefix(stage_id, market=market) + "/handoff.json")
    if saved is None or hashlib.sha256(saved.payload).hexdigest() != entry["manifest_sha256"]:
        raise LakePublishError("fixed source handoff manifest changed")
    load_handoff(store, stage_id, directory, market=market)


def seal_integrated(
    store: ObjectStore, entry: dict[str, object], *, generations: dict[str, str], recovery: bool,
) -> None:
    key = stage_prefix(str(entry["stage_id"]), market=bool(entry["market"])) + "/integrated.json"
    value = json.dumps({
        "manifest_sha256": entry["manifest_sha256"], "canonical_generations": generations,
        "status": "needs_explicit_recovery" if recovery else "integrated",
        "integrated_at": datetime.now(UTC).isoformat(),
    }, sort_keys=True).encode()
    prior = read_private(store, key)
    if prior is None:
        put_private(store, key, value, previous_etag=None)
    elif json.loads(prior.payload)["manifest_sha256"] != entry["manifest_sha256"]:
        raise LakePublishError("integration marker differs from the fixed source handoff")
