"""Freeze available private handoffs at integration start; no provider completion barrier."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

from baibai_engine.data_collection.providers.articles.contracts import SOURCES as ARTICLE_SOURCES
from baibai_engine.data_collection.providers.tradingview.market_models import MARKET_GROUP
from baibai_engine.storage.objects.json import put_private, read_write_once_private
from baibai_engine.storage.objects.r2 import (
    LakeCASConflict,
    LakePublishError,
    ObjectStore,
    R2Error,
)
from baibai_jobs.acquisition.economic_staging import (
    FRED_HANDOFF_NAMESPACE,
    is_current_fred_manifest,
    is_fred_handoff,
    is_retired_jquants_handoff,
    load_handoff,
    stage_prefix,
)

_FRED_SOURCE = "economic-fred_csv"


class ReceiptStore(ObjectStore, Protocol):
    def iter_objects(self, prefix: str) -> Iterator[tuple[str, int]]: ...


def _integration_marker(
    payload: bytes, *, digest: str, source: str, started_at: datetime, strict_source: bool
) -> dict[str, object]:
    marker = json.loads(payload)
    if not isinstance(marker, dict):
        raise ValueError("invalid integration marker")
    # The first non-article writer omitted both source and start; the manifest
    # digest still binds both. Never infer an identity that explicitly disagrees.
    legacy = not strict_source and "source" not in marker and "started_at" not in marker
    if (
        marker.get("manifest_sha256") != digest
        or (not legacy and marker.get("source") != source)
        or marker.get("status") not in ("integrated", "needs_explicit_recovery")
    ):
        raise ValueError("integration marker differs from the fixed source handoff")
    if not legacy:
        clock = marker.get("started_at")
        if not isinstance(clock, str) or datetime.fromisoformat(clock) != started_at:
            raise ValueError("integration marker start differs from the fixed source handoff")
    return marker


def available_handoffs(
    store: ReceiptStore,
    *,
    source: str | None = None,
    include_integrated: bool = False,
    market: bool | None = None,
    namespace: str | None = None,
    exclude_equity_tradingview: bool = False,
) -> list[dict[str, object]]:
    """Freeze available receipts and source ordering; isolate malformed handoffs."""
    if namespace is not None and (namespace != "article-sources" or market is not True):
        raise ValueError("explicit handoff namespace requires article market receipts")
    ready: list[dict[str, object]] = []
    watermarks: dict[str, datetime] = {}
    blocked_sources: set[str] = set()

    def inspect(
        item: tuple[bool, str, str],
    ) -> tuple[dict[str, object] | None, tuple[str, datetime] | None, str | None]:
        market, namespace, key = item
        article = namespace == "article-sources"
        fred = namespace == FRED_HANDOFF_NAMESPACE
        scope = {"namespace": namespace} if article or fred else {}
        stage_id = key.split("/")[-2]
        source_name = source or f"{namespace}-unreadable"
        digest = ""
        try:
            if (
                fred
                and key
                != f"{stage_prefix(stage_id, namespace=FRED_HANDOFF_NAMESPACE)}/handoff.json"
            ):
                raise ValueError("invalid FRED receipt path")
            saved = read_write_once_private(store, key)
            if saved is None:
                raise ValueError("listed handoff is missing")
            digest = hashlib.sha256(saved.payload).hexdigest()
            manifest = json.loads(saved.payload)
            report = manifest["result"]
            source_name = report["source"] if market else "economic-" + report["group"]
            # The generic stage ID is not proof of source. Only the manifest's
            # declaration can retire a known handoff; malformed entries remain visible.
            if (
                exclude_equity_tradingview
                and namespace == "market-sources"
                and source_name == "tradingview"
            ):
                return None, None, None
            if not isinstance(source_name, str) or (source is not None and source_name != source):
                raise ValueError("handoff source differs from its scope")
            if article and source_name not in ARTICLE_SOURCES:
                raise ValueError("unsupported article handoff source")
            if fred:
                if source_name != _FRED_SOURCE or not is_current_fred_manifest(manifest):
                    raise ValueError("unsupported live FRED receipt format")
            elif not market and source_name == _FRED_SOURCE:
                # Some old manual IDs lack the standard prefix; never read their terminal.
                return None, None, None
            started = datetime.fromisoformat(report["started_at"])
            if started.tzinfo is None:
                raise ValueError("handoff clock has no timezone")
            date.fromisoformat(report["asof"])
            terminal = read_write_once_private(
                store,
                stage_prefix(stage_id, market=market, namespace=namespace) + "/integrated.json",
            )
            integrated = None
            watermark = None
            if terminal:
                try:
                    integrated = _integration_marker(
                        terminal.payload,
                        digest=digest,
                        source=source_name,
                        started_at=started,
                        strict_source=article or fred,
                    )
                    status = integrated["status"]
                except (ValueError, KeyError, TypeError):
                    return (
                        {
                            "market": market,
                            **scope,
                            "stage_id": stage_id,
                            "source": source_name,
                            "manifest_sha256": digest,
                            "invalid": "ValueError",
                        },
                        None,
                        source_name,
                    )
                if status == "integrated":
                    watermark = (source_name, started)
                if not include_integrated:
                    return None, watermark, None
            return (
                {
                    "market": market,
                    **scope,
                    "stage_id": stage_id,
                    "manifest_sha256": digest,
                    "source": source_name,
                    "started_at": started.astimezone(UTC).isoformat(),
                    "asof": report["asof"],
                    "integration": integrated,
                    **(
                        {"previous_stage": report["planning"]["previous_stage"]}
                        if article
                        and isinstance(report.get("planning"), dict)
                        and "previous_stage" in report["planning"]
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
                    **scope,
                    "stage_id": stage_id,
                    "source": source_name,
                    "manifest_sha256": digest,
                    "invalid": type(error).__name__,
                },
                None,
                None,
            )

    keys: list[tuple[bool, str, str]] = []
    selections = (False, True) if market is None else (market,)
    for market in selections:
        selected_namespace = namespace or ("market-sources" if market else "economic-sources")
        if source is not None and source.startswith("economic-") == market:
            continue
        if not market and source in (None, _FRED_SOURCE):
            keys.extend(
                (False, FRED_HANDOFF_NAMESPACE, key)
                for key, _ in store.iter_objects(f"coordination/{FRED_HANDOFF_NAMESPACE}/")
                if key.endswith("/handoff.json")
            )
            if source == _FRED_SOURCE:
                continue
        selection = source.removeprefix("economic-") + "-" if source is not None else ""
        keys.extend(
            (market, selected_namespace, key)
            for key, _ in store.iter_objects(f"coordination/{selected_namespace}/{selection}")
            if key.endswith("/handoff.json")
            and not is_retired_jquants_handoff(key)
            and (market or not is_fred_handoff(key.split("/")[-2]))
        )
    # Only create-once manifests and terminal markers are read here. Merge in listing order.
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
        name = str(entry["source"])
        if (
            entry.get("namespace") != "article-sources"
            and entry["integration"] is None
            and name in blocked_sources
        ):
            # Ordering is unknown, not proven late. Keep siblings unsealed for
            # another attempt after the bad marker has been investigated.
            entry["invalid"] = "unverified_source_marker"
            continue
        watermark = watermarks.get(name)
        # An integrated intermediate receipt can still be missing from a stale input.
        # Only a never-integrated receipt is a newly out-of-order arrival.
        # Article receipts append independently; a later sealed stage must not hide
        # an older raw commit whose planning CAS failed or whose integration failed.
        entry["late_before_integrated"] = bool(
            entry.get("namespace") != "article-sources"
            # 個別受領IDで引用するMCP原本は、後着でも他の観測を置換しない。
            and name != "economic-" + MARKET_GROUP
            and entry["integration"] is None
            and watermark
            and datetime.fromisoformat(str(entry["started_at"])) < watermark
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
    load_handoff(
        store,
        stage_id,
        directory,
        market=market,
        namespace=namespace,
        expected_manifest_sha256=str(entry["manifest_sha256"]),
    )


def seal_integrated(
    store: ObjectStore,
    entry: dict[str, object],
    *,
    generations: dict[str, str],
    recovery: bool,
) -> None:
    namespace = str(entry["namespace"]) if entry.get("namespace") else None
    stage_id, market = str(entry["stage_id"]), bool(entry["market"])
    archived_fred = (
        not market and namespace in (None, "economic-sources") and is_fred_handoff(stage_id)
    )
    if archived_fred or (entry["source"] == _FRED_SOURCE) != (namespace == FRED_HANDOFF_NAMESPACE):
        raise ValueError("only the live FRED inbox can write a FRED terminal marker")
    prefix = stage_prefix(stage_id, market=market, namespace=namespace)
    if is_retired_jquants_handoff(prefix + "/handoff.json", source=entry["source"]):
        raise ValueError("retired J-Quants market handoffs cannot be sealed")
    terminal = (
        f"recovered-{entry['manifest_sha256']}.json"
        if (entry.get("explicit_article_recovery") or entry.get("explicit_jpx_recovery"))
        and not recovery
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
    prior = read_write_once_private(store, key)
    if prior is None:
        put_private(store, key, value, previous_etag=None)
    else:
        try:
            _integration_marker(
                prior.payload,
                digest=str(entry["manifest_sha256"]),
                source=str(entry["source"]),
                started_at=datetime.fromisoformat(str(entry["started_at"])),
                strict_source=namespace in ("article-sources", FRED_HANDOFF_NAMESPACE),
            )
        except ValueError as error:
            raise LakePublishError(
                "integration marker differs from the fixed source handoff"
            ) from error
