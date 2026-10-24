"""Freeze available private handoffs at integration start; no provider completion barrier."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
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
    """Freeze available receipts and source ordering; isolate malformed handoffs."""
    ready: list[dict[str, object]] = []
    watermarks: dict[str, datetime] = {}
    blocked_sources: set[str] = set()
    for market in (False, True):
        namespace = "market-sources" if market else "economic-sources"
        if source is not None and source.startswith("economic-") == market:
            continue
        selection = source.removeprefix("economic-") + "-" if source is not None else ""
        for key, _ in store.iter_objects(f"coordination/{namespace}/{selection}"):
            if not key.endswith("/handoff.json"):
                continue
            stage_id = key.split("/")[-2]
            source_name = source or f"{namespace}-unreadable"
            digest = ""
            try:
                saved = read_private(store, key)
                if saved is None:
                    raise ValueError("listed handoff is missing")
                digest = hashlib.sha256(saved.payload).hexdigest()
                manifest = json.loads(saved.payload)
                report = manifest["result"]
                source_name = report["source"] if market else "economic-" + report["group"]
                if not isinstance(source_name, str) or (
                    source is not None and source_name != source
                ):
                    raise ValueError("handoff source differs from its scope")
                started = datetime.fromisoformat(report["started_at"])
                if started.tzinfo is None:
                    raise ValueError("handoff clock has no timezone")
                date.fromisoformat(report["asof"])
                terminal = read_private(
                    store, stage_prefix(stage_id, market=market) + "/integrated.json"
                )
                integrated = None
                if terminal:
                    try:
                        integrated = json.loads(terminal.payload)
                        status = integrated["status"]
                    except (ValueError, KeyError, TypeError):
                        blocked_sources.add(source_name)
                        raise ValueError("source ordering marker is invalid") from None
                    if status != "needs_explicit_recovery":
                        watermarks[source_name] = max(watermarks.get(source_name, started), started)
                    if not include_integrated:
                        continue
                ready.append(
                    {
                        "market": market,
                        "stage_id": stage_id,
                        "manifest_sha256": digest,
                        "source": source_name,
                        "started_at": started.astimezone(UTC).isoformat(),
                        "asof": report["asof"],
                        "integration": integrated,
                    }
                )
            except LakePublishError as error:
                if str(error).startswith("R2 "):
                    raise
                ready.append(
                    {
                        "market": market,
                        "stage_id": stage_id,
                        "source": source_name,
                        "manifest_sha256": digest,
                        "invalid": type(error).__name__,
                    }
                )
            except (ValueError, KeyError, TypeError) as error:
                ready.append(
                    {
                        "market": market,
                        "stage_id": stage_id,
                        "source": source_name,
                        "manifest_sha256": digest,
                        "invalid": type(error).__name__,
                    }
                )
    for entry in ready:
        if entry.get("invalid"):
            continue
        watermark = watermarks.get(str(entry["source"]))
        entry["late_before_integrated"] = bool(
            str(entry["source"]) in blocked_sources
            or (watermark and datetime.fromisoformat(str(entry["started_at"])) < watermark)
        )
    return sorted(
        ready,
        key=lambda entry: (
            str(entry["source"]),
            str(entry.get("started_at", "")),
            str(entry["stage_id"]),
        ),
    )



def download_fixed(store: ObjectStore, entry: dict[str, object], directory: Path) -> None:
    stage_id, market = str(entry["stage_id"]), bool(entry["market"])
    saved = read_private(store, stage_prefix(stage_id, market=market) + "/handoff.json")
    if saved is None or hashlib.sha256(saved.payload).hexdigest() != entry["manifest_sha256"]:
        raise LakePublishError("fixed source handoff manifest changed")
    load_handoff(store, stage_id, directory, market=market)


def seal_integrated(
    store: ObjectStore,
    entry: dict[str, object],
    *,
    generations: dict[str, str],
    recovery: bool,
) -> None:
    key = stage_prefix(str(entry["stage_id"]), market=bool(entry["market"])) + "/integrated.json"
    value = json.dumps(
        {
            "manifest_sha256": entry["manifest_sha256"],
            "canonical_generations": generations,
            "source": entry["source"],
            "started_at": entry["started_at"],
            "status": "needs_explicit_recovery" if recovery else "integrated",
            "integrated_at": datetime.now(UTC).isoformat(),
        },
        sort_keys=True,
    ).encode()
    prior = read_private(store, key)
    if prior is None:
        put_private(store, key, value, previous_etag=None)
    elif json.loads(prior.payload)["manifest_sha256"] != entry["manifest_sha256"]:
        raise LakePublishError("integration marker differs from the fixed source handoff")
