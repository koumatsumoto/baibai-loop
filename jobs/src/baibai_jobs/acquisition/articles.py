"""Finite per-source article acquisition; each source saves partial L1 independently."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Literal, TypedDict
from xml.etree import ElementTree as ET  # nosec B405

import requests

from baibai_engine.data_collection.providers.articles.contracts import (
    RSS_SOURCES,
    SOURCES,
    WORLD_NEWS_SOURCES,
    NativeArticle,
    NativeLink,
)
from baibai_engine.data_collection.providers.articles.native import (
    discover_links,
    discovery_revision_for,
    next_index_url,
    parse_index,
)
from baibai_engine.data_collection.receipts.articles.contracts import (
    RSS_NATIVE_V1,
    ArticleReceipt,
    RssIndexRevision,
)
from baibai_engine.data_collection.receipts.articles.store import (
    ArticleStoreError,
    connect_read_only,
    copy_receipt_closure,
    read_body,
    read_receipt,
    read_receipt_body,
    retain_receipt,
)
from baibai_engine.data_collection.receipts.articles.store import (
    open_receipt_store as open_connection,
)
from baibai_engine.data_collection.transport.articles import ArticleBudget, fetch_article
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError, ObjectStore
from baibai_jobs.acquisition.article_discovery import (
    DISCOVERY_SOURCES,
    MAX_DISCOVERY_BYTES,
    MAX_DISCOVERY_REPLAYS,
    DiscoveryWork,
)
from baibai_jobs.acquisition.article_frontier import (
    FRONTIER_SOURCES,
    MAX_FRONTIER_BYTES,
    ArticleFrontier,
)
from baibai_jobs.acquisition.article_metrics import ArticleObjectMetrics
from baibai_jobs.acquisition.article_planning import (
    ARTICLE_NAMESPACE,
    COLLECTION_SOURCES,
    MAX_REQUEST_STATES,
    TSR_SOURCES,
    commit_stage,
    index_poll_due,
    is_due,
    load_discovery_cursor,
    load_frontier_cursor,
    load_planning,
    slot_at,
)
from baibai_jobs.acquisition.cooldown import extend, quota_key, read_deadline
from baibai_jobs.acquisition.economic_staging import stage_prefix
from baibai_jobs.acquisition.receipts import download_fixed

# トウシルは記事用rakuten queueで直列化し、公的sourceをその待機に巻き込まない。
GROUPS = {
    "public": tuple(
        source for source in COLLECTION_SOURCES if source not in WORLD_NEWS_SOURCES | {"toushiru"}
    ),
    "world": tuple(source for source in COLLECTION_SOURCES if source in WORLD_NEWS_SOURCES),
    "rakuten": ("toushiru",),
    # Only the two article sources read by default trend models run on schedule.
    "scheduled": ("tsr_data", "boj_news"),
}


# World indexes have a separate budget; they cannot consume the public group's allowance.
GROUP_BYTES = {
    "public": 80 * 1024 * 1024,
    "world": 4 * 1024 * 1024,
    "rakuten": 24 * 1024 * 1024,
}
GROUP_BYTES["scheduled"] = GROUP_BYTES["public"]


def cooldown_key(source: str) -> str:
    # Keep provider waits independent; the two TSR streams share one deadline.
    # Preserve the Rakuten key used by Toushiru and the original source identities.
    quota = {
        "minkabu_news": "minkabu",
        "rakuten_news": "rakuten",
        "toushiru": "rakuten",
        "tsr_data": "tsr",
        "tsr_flash": "tsr",
    }.get(source, source)
    return quota_key(quota)


@dataclass
class GroupBudget:
    remaining_bytes: int


def _parsed_rss_root(body: bytes) -> bool:
    # parse_index has already validated bounded UTF-8 XML without DTD/entities.
    # Its feed family accepts RDF and sitemap too; witness only an actual RSS root.
    try:
        parser = ET.iterparse(StringIO(body.decode("utf-8-sig")), events=("start",))  # nosec B314
        root = next(parser, None)
    except (ET.ParseError, LookupError, ValueError):
        return False  # Evidence must never change the normal acquisition result.
    return root is not None and root[1].tag.rsplit("}", 1)[-1] == "rss"


def acquire_source(
    store: ObjectStore,
    *,
    source: str,
    slot: datetime,
    identity: str,
    directory: Path,
    session: requests.Session,
    group_budget: GroupBudget | None = None,
    rss_native_index: bool = False,
) -> dict[str, Any]:
    started = datetime.now(UTC)
    state, etag = load_planning(store, source)
    key = cooldown_key(source)
    if state.not_before is not None and datetime.fromisoformat(state.not_before) > started:
        extend(store, key, datetime.fromisoformat(state.not_before))
    shared_deadline, _ = read_deadline(store, key)
    if shared_deadline is not None and started < shared_deadline:
        return {"source": source, "status": "not_due", "reason": "retry_not_before"}
    cursor = load_discovery_cursor(store, state) if source in DISCOVERY_SOURCES else state.cursor
    if cursor.get("omitted_url_count"):
        raise ValueError("unfinished article omissions require explicit recovery")
    discovery = DiscoveryWork(cursor) if source in DISCOVERY_SOURCES else None
    resume_pending = discovery is not None and (
        bool(cursor.get("pending_urls"))
        or any(
            item["attempted_revision"] != discovery_revision_for(source)
            for item in discovery.pending.values()
        )
    )
    if not is_due(state, slot=slot, now=started, resume_pending=resume_pending):
        return {"source": source, "status": "not_due"}
    if source in FRONTIER_SOURCES:
        cursor = load_frontier_cursor(store, state)
    policy = SOURCES[source]
    if group_budget is not None and group_budget.remaining_bytes <= 0:
        return {"source": source, "status": "deferred_budget"}
    stage_id = f"{source}-{slot:%Y%m%dT%H%M}-{identity}"
    directory.mkdir(parents=True, exist_ok=False)
    budget = ArticleBudget.start(
        max_requests=2 * (policy.max_pages + policy.max_bodies),
        max_bytes=min(policy.max_bytes, group_budget.remaining_bytes)
        if group_budget
        else policy.max_bytes,
        seconds=policy.max_seconds,
        min_interval_seconds=policy.min_interval_seconds or 5.0,
    )
    planning = dict(state.requests)
    discovery_replays = 0
    discovery_edges: dict[str, list[dict[str, Any]]] = {}
    pending_urls = list(cursor.get("pending_urls", []))
    carried_urls = set(pending_urls)
    saved_evidence = cursor.get("pending_evidence", {})
    if not isinstance(saved_evidence, dict):
        saved_evidence = {}
    receipt_ids: list[str] = []
    kinds: dict[str, str] = {}
    document_evidence: dict[str, dict[str, str]] = {}
    failed: list[str] = []
    downloaded: dict[str, Path] = {}
    cooldown = state.not_before
    pages = 0
    rss_statuses: dict[str, set[int]] = {}
    bodies = 0
    access_denied = False
    next_page: str | None = None
    partial = False
    evidence_by_url: dict[str, list[dict[str, str]]] = {}
    frontier: ArticleFrontier | None = None
    index_cache: dict[tuple[str, str], tuple[NativeArticle, ...]] = {}
    index_receipts: dict[tuple[str, str], ArticleReceipt] = {}
    policy_excluded = 0
    with closing(open_connection(directory / "receipts.sqlite")) as conn:

        def evidence_for(url: str) -> dict[str, Any] | None:
            if frontier is not None:
                return frontier.evidence(url)
            candidates = evidence_by_url.get(url, [])
            if candidates:
                identities = {(item["article_id"], item["title"]) for item in candidates}
                return (
                    {**candidates[0], "stage": {"stage_id": stage_id}}
                    if len(identities) == 1
                    else None
                )
            prior = saved_evidence.get(url)
            if not isinstance(prior, dict) or not isinstance(prior.get("stage"), dict):
                return None
            stage = prior["stage"]
            if (
                stage.get("source") != source
                or stage.get("namespace") != ARTICLE_NAMESPACE
                or stage.get("market") is not True
                or not isinstance(stage.get("stage_id"), str)
                or not isinstance(stage.get("manifest_sha256"), str)
            ):
                return None
            if any(
                not isinstance(prior.get(field), str)
                for field in ("index_receipt_id", "article_id", "native_locator", "title")
            ):
                return None
            return prior

        def attach_prior_index(link: dict[str, Any]) -> bool:
            entry = link["stage"]
            if entry["stage_id"] == stage_id:
                return True
            identity = link["index_receipt_id"]
            try:
                if conn.execute(
                    "SELECT 1 FROM article_source_receipts WHERE receipt_id=?", (identity,)
                ).fetchone():
                    original = read_receipt(conn, identity)
                else:
                    key = entry["stage_id"]
                    if key not in downloaded:
                        packet = directory / f"baseline-{len(downloaded)}"
                        download_fixed(store, entry, packet)
                        downloaded[key] = packet / "receipts.sqlite"
                    with closing(connect_read_only(downloaded[key])) as old, conn:
                        original = read_receipt(old, identity)
                        if original.source != source or original.body_ref is None:
                            raise ArticleStoreError("index evidence differs from its source")
                        copy_receipt_closure(old, conn, identity)
                if original.source != source or original.body_ref is None:
                    raise ArticleStoreError("index evidence differs from its source")
                read_body(conn, original.body_ref)
            except (LakePublishError, sqlite3.DatabaseError, OSError, KeyError, ValueError):
                return False
            if identity not in kinds:
                receipt_ids.append(identity)
                kinds[identity] = "index"
            return True

        def resolve_index(reference: dict[str, Any]) -> tuple[NativeArticle, ...]:
            entry = reference["stage"]
            receipt_id = reference["index_receipt_id"]
            key = (entry["stage_id"], receipt_id)
            if key in index_cache:
                return index_cache[key]
            if entry["stage_id"] == stage_id:
                index_receipt = read_receipt(conn, receipt_id)
                if index_receipt.body_ref is None:
                    raise ArticleStoreError("frontier index has no original body")
                original, body = read_receipt_body(conn, index_receipt)
            else:
                if (
                    entry.get("source") != source
                    or entry.get("namespace") != ARTICLE_NAMESPACE
                    or entry.get("market") is not True
                ):
                    raise ArticleStoreError("frontier index differs from its source")
                if entry["stage_id"] not in downloaded:
                    packet = directory / f"baseline-{len(downloaded)}"
                    download_fixed(store, entry, packet)
                    downloaded[entry["stage_id"]] = packet / "receipts.sqlite"
                with closing(connect_read_only(downloaded[entry["stage_id"]])) as old:
                    index_receipt = read_receipt(old, receipt_id)
                    if index_receipt.body_ref is None:
                        raise ArticleStoreError("frontier index has no original body")
                    original, body = read_receipt_body(old, index_receipt)
            if index_receipt.source != source:
                raise ArticleStoreError("frontier index differs from its source")
            items = parse_index(
                source,
                body,
                url=index_receipt.requested_url,
                encoding=index_receipt.encoding or "utf-8",
                rss_index_revision=original.rss_index_revision,
            )
            index_cache[key] = items
            index_receipts[key] = index_receipt
            return items

        def restore_discovery(reference: dict[str, Any]) -> tuple[ArticleReceipt, bytes]:
            entry = reference["stage"]
            identity = reference["receipt_id"]
            if entry.get("stage_id") != stage_id and (
                entry.get("source") != source
                or entry.get("namespace") != ARTICLE_NAMESPACE
                or entry.get("market") is not True
            ):
                raise ArticleStoreError("discovery original belongs to another source")
            if not conn.execute(
                "SELECT 1 FROM article_source_receipts WHERE receipt_id=?", (identity,)
            ).fetchone():
                key = entry["stage_id"]
                if key not in downloaded:
                    packet = directory / f"baseline-{len(downloaded)}"
                    download_fixed(store, entry, packet)
                    downloaded[key] = packet / "receipts.sqlite"
                with closing(connect_read_only(downloaded[key])) as old, conn:
                    read_receipt(old, identity, receipt_sha256=reference["receipt_sha256"])
                    copy_receipt_closure(old, conn, identity)
            original = read_receipt(conn, identity, receipt_sha256=reference["receipt_sha256"])
            if (
                original.source != source
                or original.requested_url != reference["url"]
                or original.body_ref is None
                or original.body_ref.model_dump() != reference["body_ref"]
                or (original.encoding or "utf-8") != reference["encoding"]
            ):
                raise ArticleStoreError("discovery original reference differs")
            body = read_body(conn, original.body_ref)
            if (link := reference.get("index_evidence")) is not None:
                if not attach_prior_index(link):
                    raise ArticleStoreError("discovery index evidence is unavailable")
                document_evidence[identity] = {
                    field: link[field]
                    for field in ("index_receipt_id", "article_id", "native_locator", "title")
                }
            if identity not in kinds:
                receipt_ids.append(identity)
                kinds[identity] = "document"
            return original, body

        def discover(
            identity: str, receipt: ArticleReceipt, body: bytes, queue: list[str]
        ) -> list[str]:
            nonlocal policy_excluded
            assert discovery is not None
            revision = discovery_revision_for(source)
            try:
                links = discover_links(
                    source, body, url=receipt.requested_url, encoding=receipt.encoding or "utf-8"
                )
            except ValueError:
                # Only native parsing is inside this catch. Missing/corrupt R2
                # originals are operational failures, never parser-version waits.
                discovery.unsupported(identity, revision)
                return queue
            accepted = tuple(
                link
                for link in links
                if policy.allows_url(link.url) and link.url != receipt.requested_url
            )
            policy_excluded += len(links) - len(accepted)
            admitted = discovery.admit(identity, accepted, queue, revision=revision)
            if admitted is None:
                failed.append("discovery_capacity_exceeded")
                return queue
            return admitted

        def receive(
            url: str, kind: Literal["index", "document"]
        ) -> tuple[ArticleReceipt, bytes | None, RssIndexRevision | None]:
            nonlocal access_denied, cooldown
            budget.check()
            previous = planning.get(url)
            baseline = ArticleReceipt.model_validate(previous["receipt"]) if previous else None
            if (
                previous is not None
                and baseline is not None
                and (baseline.etag or baseline.last_modified)
            ):
                try:
                    if baseline.body_ref is None:
                        raise ArticleStoreError("baseline has no original body")
                    ref = baseline.body_ref
                    if conn.execute(
                        "SELECT 1 FROM article_source_receipts WHERE receipt_id=?",
                        (ref.receipt_id,),
                    ).fetchone():
                        read_body(conn, ref)
                    else:
                        entry = previous["stage"]
                        key = entry["stage_id"]
                        if key not in downloaded:
                            packet = directory / f"baseline-{len(downloaded)}"
                            download_fixed(store, entry, packet)
                            downloaded[key] = packet / "receipts.sqlite"
                        with closing(connect_read_only(downloaded[key])) as old, conn:
                            copy_receipt_closure(old, conn, ref.receipt_id)
                except (
                    LakePublishError,
                    sqlite3.DatabaseError,
                    OSError,
                    KeyError,
                    ValueError,
                ):
                    # A broken old closure cannot authorize validators. One fresh
                    # provider GET replaces it within the ordinary request budget.
                    baseline = None
                    failed.append("baseline_unavailable")
            else:
                baseline = None
            before_bytes = budget.bytes_used
            responses = fetch_article(
                session,
                source=source,
                url=url,
                allowed_hosts=policy.allowed_hosts,
                budget=budget,
                max_response_bytes=policy.response_limit(kind),
                baseline=baseline,
            )
            if group_budget is not None:
                group_budget.remaining_bytes -= budget.bytes_used - before_bytes
            final = responses[-1].receipt
            for response in responses:
                receipt = response.receipt
                if (
                    rss_native_index
                    and kind == "index"
                    and source in RSS_SOURCES
                    and receipt.status == "ok"
                ):
                    receipt = receipt.model_copy(update={"rss_index_revision": RSS_NATIVE_V1})
                final = receipt
                with conn:
                    retain_receipt(conn, receipt, body=response.body)
                receipt_ids.append(receipt.receipt_id)
                kinds[receipt.receipt_id] = kind
                if receipt.http_status in {401, 403}:
                    # Preserve the refusal, then end this source's pass. Other
                    # URLs, headers or sessions are not a way around that result.
                    access_denied = True
                if receipt.status == "ok":
                    # Updating an existing mapping moves it to the bounded recent end.
                    planning.pop(url, None)
                    planning[url] = {
                        "receipt": receipt.model_dump(mode="json"),
                        "stage": {"stage_id": stage_id},
                    }
                elif receipt.status != "not_modified" and not (
                    kind == "document"
                    and policy.document_redirect(url, receipt.http_status, receipt.location)
                ):
                    failed.append(receipt.status)
                if receipt.cooldown_until is not None:
                    proposed = receipt.cooldown_until.isoformat()
                    if (
                        cooldown is None
                        or datetime.fromisoformat(cooldown) < receipt.cooldown_until
                    ):
                        cooldown = proposed
            if final.body_ref is None:
                return final, None, None
            if kind == "index":
                original, body = read_receipt_body(conn, final)
                return final, body, original.rss_index_revision
            return final, read_body(conn, final.body_ref), None

        if source in FRONTIER_SOURCES:
            frontier = ArticleFrontier(cursor, resolve_index)
            if frontier.encoded_size() > MAX_FRONTIER_BYTES:
                raise ValueError("saved article work exceeds the frontier bound; recover exactly")
        received_index = max(
            (
                item.received_at
                for item in index_receipts.values()
                if item.requested_url in policy.entry_urls
            ),
            default=None,
        )
        poll_due = index_poll_due(source, now=started, received_at=received_index)
        if (
            discovery is not None
            and state.last_slot is not None
            and datetime.fromisoformat(state.last_slot) == slot
        ):
            # A saved polling slot is not a completed discovery workset. Resume
            # retained parents/children without repeating that slot's index GET.
            poll_due = False
        if not poll_due and frontier is not None and not frontier.pending:
            return {"source": source, "status": "not_due", "reason": "index_and_queue_complete"}
        queue: list[str] = []
        frontier_queued = False
        active_url: str | None = None
        try:
            if discovery is not None:
                revision = discovery_revision_for(source)
                replay_started = time.monotonic()
                for identity, reference in list(discovery.pending.items()):
                    if reference["attempted_revision"] == revision:
                        continue
                    if discovery_replays >= MAX_DISCOVERY_REPLAYS or (
                        time.monotonic() - replay_started >= 30
                    ):
                        break
                    original, retained_body = restore_discovery(reference)
                    pending_urls = discover(identity, original, retained_body, pending_urls)
                    discovery_replays += 1
            # A fulfilled TSR index cycle is reused while its unfinished document
            # FIFO advances. Other feeds keep their existing overlap/revalidation.
            entry_urls = list(policy.entry_urls) if poll_due else []
            page_url: str | None = entry_urls.pop(0) if entry_urls else None
            seen_pages = set()
            discovered_by_identity: dict[str, str] = {}
            while page_url and pages < policy.max_pages:
                if page_url in seen_pages:
                    failed.append("index_page_cycle")
                    partial = True
                    break
                seen_pages.add(page_url)
                receipt, body, rss_index_revision = receive(page_url, "index")
                pages += 1
                if body is None:
                    partial = True
                    if access_denied:
                        next_page = cursor.get("next_page") if pages == 1 else page_url
                    if not access_denied and receipt.cooldown_until is None and entry_urls:
                        page_url = entry_urls.pop(0)
                        continue
                    break
                try:
                    native = parse_index(
                        source,
                        body,
                        url=page_url,
                        encoding=receipt.encoding or "utf-8",
                        rss_index_revision=rss_index_revision,
                    )
                except ValueError:
                    if source != "nhk_news" and source not in WORLD_NEWS_SOURCES:
                        raise
                    # This feed's original is saved, but another independent
                    # feed entry may still provide valid items in the same slot.
                    failed.append("index_parse_failed")
                    partial = True
                    page_url = entry_urls.pop(0) if entry_urls else None
                    continue
                status = receipt.http_status
                if (
                    source == "boj_news"
                    and status is not None
                    and (receipt.status, status) in (("ok", 200), ("not_modified", 304))
                    and _parsed_rss_root(body)
                ):
                    # A bound 304 uses its actual original 200's revision, not this run's flag.
                    field = (
                        "rss_index_http_statuses"
                        if rss_index_revision == RSS_NATIVE_V1
                        else "rss_unversioned_index_http_statuses"
                    )
                    rss_statuses.setdefault(field, set()).add(status)
                if frontier is not None:
                    reference = {
                        "stage": {"stage_id": stage_id},
                        "index_receipt_id": receipt.receipt_id,
                    }
                    index_cache[(stage_id, receipt.receipt_id)] = native
                    frontier.observe(page_url, reference, native)
                for item in native:
                    fetch_url = str(item.metadata.get("fetch_url") or item.url)
                    discovered_by_identity.setdefault(item.article_id, fetch_url)
                    evidence_by_url.setdefault(fetch_url, []).append(
                        {
                            "index_receipt_id": receipt.receipt_id,
                            "article_id": item.article_id,
                            "native_locator": item.locator,
                            "title": item.title,
                        }
                    )
                next_page = next_index_url(
                    source, body, url=page_url, encoding=receipt.encoding or "utf-8"
                )
                resume_page = cursor.get("next_page") if pages == 1 else None
                page_url = entry_urls.pop(0) if entry_urls else resume_page or next_page
                if page_url in seen_pages:
                    failed.append("index_page_cycle")
                    partial = True
                    break
            index_complete = bool(pages) and not partial and not page_url and not entry_urls
            discovered = list(discovered_by_identity.values())
            fresh = [url for url in discovered if url not in planning]
            # Two recent known URLs are revalidated even without sitemap lastmod.
            recent_after = started - timedelta(hours=policy.revalidate_hours)
            known = [
                url
                for url in discovered
                if url in planning
                and datetime.fromisoformat(planning[url]["receipt"]["received_at"]) >= recent_after
            ][: policy.max_revalidate_bodies]
            queue = (
                (
                    frontier.queue(known)
                    if frontier is not None
                    else list(dict.fromkeys([*known, *pending_urls, *fresh]))
                )
                if policy.max_bodies
                else []
            )
            if frontier is not None:
                queue = queue[: policy.max_bodies]
                frontier_queued = True
            pending_urls = []
            seen_bodies: set[str] = set()
            redirect_ancestors: dict[str, set[str]] = {}
            while not access_denied and queue and bodies < policy.max_bodies:
                if discovery is not None and discovery.encoded_size(queue) > MAX_DISCOVERY_BYTES:
                    failed.append("discovery_capacity_exceeded")
                    partial = True
                    break
                if cooldown and datetime.fromisoformat(cooldown) > datetime.now(UTC):
                    partial = True
                    break
                url = queue.pop(0)
                active_url = url
                if url in seen_bodies:
                    active_url = None
                    continue
                if not policy.allows_url(url):
                    active_url = None
                    policy_excluded += 1
                    if frontier is not None:
                        frontier.pending.pop(url, None)
                    continue  # Alias/login/foreign-host identities do not expand crawling.
                seen_bodies.add(url)
                parent_links = discovery.children.get(url, []) if discovery is not None else []
                for edge in parent_links:
                    restore_discovery(edge["parent"])
                link = evidence_for(url) if frontier is not None else None
                receipt, body, _ = receive(url, "document")
                bodies += 1
                target = policy.document_redirect(url, receipt.http_status, receipt.location)
                if target is not None:
                    # 移転元と先は別の原受領。全hopを既存の文書数/通信予算へ数える。
                    ancestors = redirect_ancestors.get(url, set())
                    if target in ancestors:
                        failed.append("document_redirect_cycle")
                        pending_urls.append(url)
                    else:
                        redirect_ancestors.setdefault(target, set()).update(ancestors | {url})
                        if target not in seen_bodies:
                            queue = list(dict.fromkeys([target, *queue]))
                    active_url = None
                    continue
                if parent_links:
                    discovery_edges[receipt.receipt_id] = parent_links
                if frontier is None:
                    link = evidence_for(url)
                bound = link is not None and attach_prior_index(link)
                if bound:
                    assert link is not None
                    document_evidence[receipt.receipt_id] = {
                        field: link[field]
                        for field in ("index_receipt_id", "article_id", "native_locator", "title")
                    }
                elif source == "nhk_news" or frontier is not None:
                    # Its GUID may differ from the URL. Keep raw and mark the
                    # unresolved identity rather than inventing a URL identity.
                    failed.append("document_identity_unbound")
                if frontier is not None:
                    frontier.complete(url, success=body is not None and bound)
                if body is None:
                    # A vanished feed item is not an endless crawl obligation.
                    # Keep its failed receipt; a future relisting can rediscover it.
                    stale_missing = (
                        source == "toushiru"
                        and url in carried_urls
                        and index_complete
                        and url not in discovered
                        and receipt.http_status == 404
                    )
                    if stale_missing:
                        planning.pop(url, None)
                    else:
                        pending_urls.append(url)
                elif discovery is not None:
                    identity = discovery.register(
                        receipt, {"stage_id": stage_id}, link if bound else None
                    )
                    queue = discover(identity, receipt, body, queue)
                    discovery.children.pop(url, None)
                active_url = None
                if receipt.http_status in {401, 403} or receipt.cooldown_until is not None:
                    partial = True
                    break
            # Failed documents must not consume the same bounded slots forever.
            # Refusals/cooldowns keep their stop point; only a finished pass rotates.
            pending_urls = [*pending_urls, *queue] if partial else [*queue, *pending_urls]
        except Exception as error:
            # No source text/URL/error messages enter logs. Completed L1 remains
            # eligible for staging even if a later request or native parse fails.
            failed.append(type(error).__name__)
            if frontier is not None and active_url is not None:
                frontier.complete(active_url, success=False)
            pending_urls = list(
                dict.fromkeys([*pending_urls, *([active_url] if active_url else []), *queue])
            )
            partial = True
        if frontier is not None:
            if not frontier_queued:
                frontier.queue([])
            frontier.next_page = next_page
            pending_urls = list(frontier.pending)
            if frontier.capacity_blocked:
                failed.append("frontier_capacity_exceeded")
            cursor = frontier.wire()
        else:
            # Keep all unfinished URLs and evidence, even after validator eviction.
            pending_urls = list(dict.fromkeys(pending_urls))
            cursor = {
                "pending_urls": pending_urls,
                "pending_evidence": {
                    url: link for url in pending_urls if (link := evidence_for(url)) is not None
                },
                "next_page": next_page,
            }
            if discovery is not None:
                cursor = discovery.wire(cursor)
                if discovery.pending:
                    failed.append("discovery_pending")
        continuation_only = bool(pending_urls or next_page) and not (failed or partial)
        # Current index and recent bodies only; old raw history stays in immutable stages.
        planning = dict(list(planning.items())[-MAX_REQUEST_STATES:])
        report = {
            "source": source,
            "asof": slot.astimezone(timezone(timedelta(hours=9))).date().isoformat(),
            "started_at": started.isoformat(),
            "slot": slot.isoformat(),
            "receipt_ids": receipt_ids,
            "projection_kinds": kinds,
            "document_evidence": document_evidence,
            "discovery_edges": discovery_edges,
            "discovery_replays": discovery_replays,
            "discovery_pending_count": len(discovery.pending) if discovery is not None else 0,
            "status": "partial" if failed or partial or pending_urls or next_page else "completed",
            "continuation_only": continuation_only,
            "failures": sorted(set(failed)),
            "pages": pages,
            "index_reused": not poll_due,
            "bodies": bodies,
            "pending_url_count": len(pending_urls),
            "policy_excluded_count": policy_excluded,
            "requests_used": budget.requests_used,
            "bytes_used": budget.bytes_used,
            "acquisition_seconds": round((datetime.now(UTC) - started).total_seconds(), 3),
        }
    write_json_atomic(directory / "result.json", report)
    try:
        saved_stage = commit_stage(
            store,
            state=state,
            expected_etag=etag,
            slot=slot,
            stage_id=stage_id,
            directory=directory,
            requests=planning,
            cursor=cursor,
            not_before=cooldown,
        )
    finally:
        # A saved raw handoff can outlive a failed cursor CAS. Preserve its quota
        # wait independently so another source/run cannot immediately request again.
        if cooldown is not None and datetime.fromisoformat(cooldown) > datetime.now(UTC):
            extend(store, key, datetime.fromisoformat(cooldown))
    return {
        **{
            key: value
            for key, value in report.items()
            if key
            not in {"projection_kinds", "receipt_ids", "document_evidence", "discovery_edges"}
        },
        "stage_id": saved_stage["stage_id"],
        "manifest_sha256": saved_stage["manifest_sha256"],
        **{field: sorted(statuses) for field, statuses in rss_statuses.items()},
    }


_BOJ_PENDING_COUNT = 9  # One frozen cutover workset, never an all-history audit.
_BOJ_MAX_EVIDENCE_REFS = 2 * _BOJ_PENDING_COUNT


class _BojPendingProof(TypedDict):
    source: str
    stage_id: str
    manifest_sha256: str
    pending_url_count: int
    native_seed_count: int
    native_child_count: int
    incompatible_count: int
    unverified_count: int
    unverified_reason_counts: dict[str, int]


type _IndexItems = set[tuple[str, str, str, str]]


def _index_items(items: tuple[NativeArticle, ...]) -> _IndexItems:
    return {
        (str(item.metadata.get("fetch_url") or item.url), item.article_id, item.locator, item.title)
        for item in items
        if item.metadata.get("format") == "rss"
    }


class _BojPendingWitness:
    source = "boj_news"
    policy = SOURCES[source]

    def __init__(self, store: ObjectStore, scratch: Path) -> None:
        self.store = store
        self.scratch = scratch
        self.packets: dict[tuple[str, str], tuple[Path, dict[str, str]]] = {}
        self.indexes: dict[tuple[str, str, str], tuple[_IndexItems, _IndexItems] | None] = {}
        self.parent_links: dict[str, frozenset[NativeLink]] = {}

    def receipt_key(self, entry: Any, receipt_id: Any) -> tuple[str, str, str]:
        if not isinstance(entry, dict) or (
            entry.get("source") != self.source
            or entry.get("namespace") != ARTICLE_NAMESPACE
            or entry.get("market") is not True
            or not all(
                isinstance(value, str)
                for value in (entry.get("stage_id"), entry.get("manifest_sha256"), receipt_id)
            )
            or re.fullmatch(r"[0-9a-f]{64}", entry["manifest_sha256"]) is None
        ):
            raise ValueError("invalid fixed BOJ receipt reference")
        return entry["stage_id"], entry["manifest_sha256"], receipt_id

    def read(
        self,
        entry: dict[str, Any],
        receipt_id: str,
        *,
        kind: Literal["index", "document"],
        digest: str | None = None,
    ) -> tuple[ArticleReceipt, ArticleReceipt, bytes]:
        stage, manifest, _ = self.receipt_key(entry, receipt_id)
        ref = stage, manifest
        if ref not in self.packets:
            if len(self.packets) >= _BOJ_MAX_EVIDENCE_REFS:
                raise LakePublishError("BOJ pending evidence exceeds its fixed packet bound")
            folder = self.scratch / f"packet-{len(self.packets)}"
            download_fixed(self.store, entry, folder)
            report = json.loads((folder / "result.json").read_text(encoding="utf-8"))
            kinds = report.get("projection_kinds")
            identities = report.get("receipt_ids")
            if (
                report.get("source") != self.source
                or not isinstance(kinds, dict)
                or not isinstance(identities, list)
                or any(
                    not isinstance(identity, str) or not isinstance(kinds.get(identity), str)
                    for identity in identities
                )
            ):
                raise LakePublishError("BOJ pending evidence packet report is invalid")
            self.packets[ref] = (
                folder / "receipts.sqlite",
                {identity: kinds[identity] for identity in identities},
            )
        path, kinds = self.packets[ref]
        if kinds.get(receipt_id) != kind:
            raise ValueError("fixed BOJ evidence receipt has a different purpose")
        with closing(connect_read_only(path)) as conn:
            receipt = read_receipt(conn, receipt_id, receipt_sha256=digest)
            original, body = read_receipt_body(conn, receipt)
        if (
            receipt.source != self.source
            or (receipt.status, receipt.http_status) not in (("ok", 200), ("not_modified", 304))
            or original.source != receipt.source
            or original.requested_url != receipt.requested_url
            or original.representation != receipt.representation
            or original.received_at > receipt.received_at
            or original.encoding != receipt.encoding
            or original.http_status != 200
        ):
            raise ValueError("fixed BOJ body owner differs from its receipt")
        return receipt, original, body

    def index_result(self, url: str, link: Any) -> str:
        try:
            entry, receipt_id = link["stage"], link["index_receipt_id"]
            key = self.receipt_key(entry, receipt_id)
            item = (url, link["article_id"], link["native_locator"], link["title"])
            if key not in self.indexes:
                receipt, original, body = self.read(entry, receipt_id, kind="index")
                if receipt.requested_url not in self.policy.entry_urls:
                    return "index_reference_unverified"
                url_args = {"url": receipt.requested_url, "encoding": original.encoding or "utf-8"}
                recorded = _index_items(
                    parse_index(
                        self.source,
                        body,
                        rss_index_revision=original.rss_index_revision,
                        **url_args,
                    )
                )
                if not _parsed_rss_root(body):
                    self.indexes[key] = None
                else:
                    current = (
                        recorded
                        if original.rss_index_revision == RSS_NATIVE_V1
                        else _index_items(
                            parse_index(
                                self.source, body, rss_index_revision=RSS_NATIVE_V1, **url_args
                            )
                        )
                    )
                    self.indexes[key] = recorded, current
            indexes = self.indexes[key]
            if indexes is None:
                return "index_not_rss"
            recorded, current = indexes
            if item in recorded:
                return "native_seed" if item in current else "incompatible"
        except (KeyError, TypeError, ValueError, sqlite3.DatabaseError):
            return "index_reference_unverified"
        return "index_item_unverified"

    def child_result(self, url: str, edge: Any) -> str:
        if not isinstance(edge, dict) or edge.get("discovery_revision") != discovery_revision_for(
            self.source
        ):
            return "discovery_revision_unverified"
        try:
            parent = edge["parent"]
            parent_url, digest = parent["url"], parent["receipt_sha256"]
            if (
                not isinstance(parent_url, str)
                or not self.policy.allows_url(parent_url)
                or parent_url == url
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                return "parent_receipt_unverified"
            receipt, _, body = self.read(
                parent["stage"], parent["receipt_id"], kind="document", digest=digest
            )
            if (
                receipt.requested_url != parent_url
                or receipt.body_ref is None
                or receipt.body_ref.model_dump() != parent["body_ref"]
                or (receipt.encoding or "utf-8") != parent["encoding"]
            ):
                return "parent_receipt_unverified"
        except (KeyError, TypeError, ValueError, sqlite3.DatabaseError):
            return "parent_receipt_unverified"
        # The ordinary writer restores every attached parent and its recorded index evidence.
        # A parent without a proven current RSS origin is not a one-hop native proof.
        if self.index_result(parent_url, parent.get("index_evidence")) != "native_seed":
            return "parent_index_unverified"
        try:
            if digest not in self.parent_links:
                self.parent_links[digest] = frozenset(
                    discover_links(self.source, body, url=parent_url, encoding=parent["encoding"])
                )
            if NativeLink(edge["locator"], url) in self.parent_links[digest]:
                return "native_child"
        except (KeyError, LookupError, TypeError, ValueError):
            pass
        return "child_locator_unverified"

    def result(self, url: str, evidence: dict[str, Any], edges: list[Any]) -> str:
        if not self.policy.allows_url(url):
            return "url_policy_mismatch"
        if url in evidence:
            direct = self.index_result(url, evidence[url])
            if direct != "native_seed" or not edges:
                return direct
        if not edges:
            return "no_retained_evidence"
        for edge in edges:
            result = self.child_result(url, edge)
            if result != "native_child":
                return result
        return "native_child"


def _verify_boj_pending(
    store: ObjectStore, *, stage_id: str, manifest_sha256: str
) -> _BojPendingProof:
    """Verify native seeds and retained one-hop children, without modifying or fetching them."""
    source = _BojPendingWitness.source
    stage_prefix(stage_id, namespace=ARTICLE_NAMESPACE)
    if re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
        raise ValueError("BOJ verification requires an exact manifest digest")
    state, etag = load_planning(store, source)
    root = state.last_stage
    if (
        root is None
        or root.get("stage_id") != stage_id
        or root.get("manifest_sha256") != manifest_sha256
        or state.cursor.get("stage") != root
    ):
        raise ValueError("BOJ verification differs from the current planning stage")
    cursor = load_discovery_cursor(store, state)
    pending = cursor["pending_urls"]
    if (
        not isinstance(pending, list)
        or len(pending) != _BOJ_PENDING_COUNT
        or not all(isinstance(url, str) for url in pending)
        or len(set(pending)) != len(pending)
        or cursor.get("omitted_url_count")
        or cursor.get("next_page")
        or cursor.get("discoveries")
    ):
        raise ValueError("BOJ verification requires the exact bounded pending workset")
    evidence = cursor.get("pending_evidence", {})
    children = cursor["discovery_children"]
    edges = [children.get(url, []) for url in pending]
    if (
        not all(isinstance(attached, list) for attached in edges)
        or sum(map(len, edges)) > _BOJ_MAX_EVIDENCE_REFS
    ):
        raise ValueError("BOJ pending evidence exceeds its fixed child edge bound")
    with tempfile.TemporaryDirectory(prefix="boj-pending-proof-") as scratch:
        witness = _BojPendingWitness(store, Path(scratch))
        counts = Counter(
            witness.result(url, evidence, attached)
            for url, attached in zip(pending, edges, strict=True)
        )
    if load_planning(store, source) != (state, etag):
        raise LakePublishError("BOJ planning changed during verification")
    seeds, native_children, incompatible = (
        counts.pop(reason, 0) for reason in ("native_seed", "native_child", "incompatible")
    )
    return {
        "source": source,
        "stage_id": stage_id,
        "manifest_sha256": manifest_sha256,
        "pending_url_count": len(pending),
        "native_seed_count": seeds,
        "native_child_count": native_children,
        "incompatible_count": incompatible,
        "unverified_count": counts.total(),
        "unverified_reason_counts": dict(counts),
    }


def _article_store() -> Boto3R2Store:
    return Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")


def _verify_boj_pending_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="baibai-jobs acquisition.articles verify-boj-pending")
    parser.add_argument("--stage-id", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        with closing(_article_store()) as store:
            proof = _verify_boj_pending(
                store, stage_id=args.stage_id, manifest_sha256=args.manifest_sha256
            )
    except Exception as error:
        print(f"BOJ pending verification failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(proof, sort_keys=True))
    return int(
        proof["native_seed_count"] + proof["native_child_count"] != proof["pending_url_count"]
    )


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["verify-boj-pending"]:
        return _verify_boj_pending_main(arguments[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=tuple(GROUPS), required=True)
    parser.add_argument("--created-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--rss-native-index", action="store_true")
    args = parser.parse_args(arguments)
    if args.created_at.tzinfo is None or args.created_at > datetime.now(UTC):
        parser.error("created-at must be a past timezone-aware clock")
    reports = []
    group_budget = GroupBudget(GROUP_BYTES[args.group])
    with closing(_article_store()) as raw_store:
        store = ArticleObjectMetrics(raw_store)
        started = time.monotonic()
        with requests.Session() as session:
            for source in GROUPS[args.group]:
                try:
                    report = acquire_source(
                        store,
                        source=source,
                        slot=slot_at(
                            datetime.now(UTC) if source in TSR_SOURCES else args.created_at
                        ),
                        identity=args.identity,
                        directory=args.directory / source,
                        session=session,
                        group_budget=group_budget,
                        rss_native_index=args.rss_native_index,
                    )
                except Exception as error:
                    report = {"source": source, "status": "failed", "reason": type(error).__name__}
                    descriptor = args.directory / source / "stage.json"
                    if descriptor.exists():
                        saved = json.loads(descriptor.read_text())
                        report.update(
                            {
                                "stage_id": saved["stage_id"],
                                "manifest_sha256": saved["manifest_sha256"],
                                "private_saved_planning_failed": True,
                            }
                        )
                reports.append(report)
    print(
        json.dumps(
            {
                "sources": reports,
                "object_io": store.counts,
                "runner_process_seconds": round(time.monotonic() - started, 3),
            },
            sort_keys=True,
        )
    )
    return int(
        any(
            item["status"] in {"failed", "deferred_budget"}
            or (
                item["status"] == "partial"
                and (bool(item.get("failures")) or not item.get("continuation_only", False))
            )
            for item in reports
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
