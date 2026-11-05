"""Freeze available private handoffs at integration start; no provider completion barrier."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

from baibai_engine.data_collection.providers.tradingview.market_models import MARKET_GROUP
from baibai_engine.storage.objects.r2 import (
    LakeCASConflict,
    LakePublishError,
    ObjectStore,
    R2Error,
)
from baibai_jobs.acquisition.economic_staging import load_handoff, stage_prefix
from baibai_jobs.acquisition.external_assessments.staging import put_private, read_private


class ReceiptStore(ObjectStore, Protocol):
    def iter_objects(self, prefix: str) -> Iterator[tuple[str, int]]: ...


def available_handoffs(
    store: ReceiptStore,
    *,
    source: str | None = None,
    include_integrated: bool = False,
    market: bool | None = None,
) -> list[dict[str, object]]:
    """Freeze available receipts and source ordering; isolate malformed handoffs."""
    ready: list[dict[str, object]] = []
    watermarks: dict[str, datetime] = {}
    blocked_sources: set[str] = set()

    def inspect(
        item: tuple[bool, str],
    ) -> tuple[dict[str, object] | None, tuple[str, datetime] | None, str | None]:
        market, key = item
        namespace = "market-sources" if market else "economic-sources"
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
            if not isinstance(source_name, str) or (source is not None and source_name != source):
                raise ValueError("handoff source differs from its scope")
            started = datetime.fromisoformat(report["started_at"])
            if started.tzinfo is None:
                raise ValueError("handoff clock has no timezone")
            date.fromisoformat(report["asof"])
            terminal = read_private(
                store, stage_prefix(stage_id, market=market) + "/integrated.json"
            )
            integrated = None
            watermark = None
            if terminal:
                try:
                    integrated = json.loads(terminal.payload)
                    status = integrated["status"]
                except (ValueError, KeyError, TypeError):
                    return (
                        {
                            "market": market,
                            "stage_id": stage_id,
                            "source": source_name,
                            "manifest_sha256": digest,
                            "invalid": "ValueError",
                        },
                        None,
                        source_name,
                    )
                if status != "needs_explicit_recovery":
                    watermark = (source_name, started)
                if not include_integrated:
                    return None, watermark, None
            return (
                {
                    "market": market,
                    "stage_id": stage_id,
                    "manifest_sha256": digest,
                    "source": source_name,
                    "started_at": started.astimezone(UTC).isoformat(),
                    "asof": report["asof"],
                    "integration": integrated,
                    **(
                        {"request_ids_complete": True}
                        if report.get("request_ids_complete") is True
                        else {}
                    ),
                    **(
                        {"request_ids": report["request_ids"]}
                        if source_name.startswith("jquants-")
                        and isinstance(report.get("request_ids"), list)
                        and all(isinstance(value, str) for value in report["request_ids"])
                        else {}
                    ),
                },
                watermark,
                None,
            )
        except (R2Error, LakeCASConflict):
            raise
        except (LakePublishError, ValueError, KeyError, TypeError) as error:
            return (
                {
                    "market": market,
                    "stage_id": stage_id,
                    "source": source_name,
                    "manifest_sha256": digest,
                    "invalid": type(error).__name__,
                },
                None,
                None,
            )

    keys: list[tuple[bool, str]] = []
    selections = (False, True) if market is None else (market,)
    for market in selections:
        namespace = "market-sources" if market else "economic-sources"
        if source is not None and source.startswith("economic-") == market:
            continue
        selection = source.removeprefix("economic-") + "-" if source is not None else ""
        keys.extend(
            (market, key)
            for key, _ in store.iter_objects(f"coordination/{namespace}/{selection}")
            if key.endswith("/handoff.json")
        )
    # Private reads verify each object before and after downloading; independent
    # receipts do not require serial network round trips. Merge results in listing order.
    with ThreadPoolExecutor(max_workers=8) as workers:
        for entry, mark, blocked in workers.map(inspect, keys):
            if entry is not None:
                ready.append(entry)
            if mark is not None:
                name, started = mark
                watermarks[name] = max(watermarks.get(name, started), started)
            if blocked is not None:
                blocked_sources.add(blocked)
    for entry in ready:
        if entry.get("invalid"):
            continue
        watermark = watermarks.get(str(entry["source"]))
        # An integrated intermediate receipt can still be missing from a stale input.
        # Only a never-integrated receipt is a newly out-of-order arrival.
        entry["late_before_integrated"] = bool(
            str(entry["source"]) in blocked_sources
            or (
                # 個別受領IDで引用するMCP原本は、後着でも他の観測を置換しない。
                str(entry["source"]) != "economic-" + MARKET_GROUP
                and entry["integration"] is None
                and watermark
                and datetime.fromisoformat(str(entry["started_at"])) < watermark
            )
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
    namespace = str(entry["namespace"]) if entry.get("namespace") else None
    saved = read_private(
        store, stage_prefix(stage_id, market=market, namespace=namespace) + "/handoff.json"
    )
    if saved is None or hashlib.sha256(saved.payload).hexdigest() != entry["manifest_sha256"]:
        raise LakePublishError("fixed source handoff manifest changed")
    load_handoff(store, stage_id, directory, market=market, namespace=namespace)


def seal_integrated(
    store: ObjectStore,
    entry: dict[str, object],
    *,
    generations: dict[str, str],
    recovery: bool,
) -> None:
    namespace = str(entry["namespace"]) if entry.get("namespace") else None
    prefix = stage_prefix(str(entry["stage_id"]), market=bool(entry["market"]), namespace=namespace)
    terminal = (
        f"recovered-{entry['manifest_sha256']}.json"
        if entry.get("explicit_article_recovery") and not recovery
        else "integrated.json"
    )
    key = prefix + "/" + terminal
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
