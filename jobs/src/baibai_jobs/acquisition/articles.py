"""Finite per-source article acquisition; each source saves partial L1 independently."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import requests

from baibai_engine.data_collection.providers.articles.contracts import SOURCES
from baibai_engine.data_collection.providers.articles.native import (
    next_index_url,
    parse_document,
    parse_index,
)
from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt
from baibai_engine.data_collection.receipts.articles.store import (
    ArticleStoreError,
    copy_receipt_closure,
    read_body,
    read_receipt,
    retain_receipt,
)
from baibai_engine.data_collection.transport.articles import ArticleBudget, fetch_article
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError, ObjectStore
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.article_metrics import ArticleObjectMetrics
from baibai_jobs.acquisition.article_planning import (
    ARTICLE_NAMESPACE,
    MAX_REQUEST_STATES,
    commit_stage,
    is_due,
    load_planning,
    slot_at,
)
from baibai_jobs.acquisition.receipts import download_fixed

# The two shared hosts use the same Actions concurrency keys as the existing
# external-assessment collector. Public providers do not wait behind that work.
GROUPS = {
    "public": tuple(
        source for source in SOURCES if source not in {"minkabu_news", "rakuten_news", "toushiru"}
    ),
    "minkabu": ("minkabu_news",),
    "rakuten": ("rakuten_news", "toushiru"),
}


# Independently allocated group caps total 128 MiB/slot; no shared mutable quota.
GROUP_BYTES = {"public": 80 * 1024 * 1024, "minkabu": 24 * 1024 * 1024, "rakuten": 24 * 1024 * 1024}


@dataclass
class GroupBudget:
    remaining_bytes: int


def acquire_source(
    store: ObjectStore,
    *,
    source: str,
    slot: datetime,
    identity: str,
    directory: Path,
    session: requests.Session,
    group_budget: GroupBudget | None = None,
) -> dict[str, Any]:
    started = datetime.now(UTC)
    state, etag = load_planning(store, source)
    if not is_due(state, slot=slot, now=started):
        return {"source": source, "status": "not_due"}
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
    pending_urls = list(state.cursor.get("pending_urls", []))
    saved_evidence = state.cursor.get("pending_evidence", {})
    if not isinstance(saved_evidence, dict):
        saved_evidence = {}
    receipt_ids: list[str] = []
    kinds: dict[str, str] = {}
    document_evidence: dict[str, dict[str, str]] = {}
    failed: list[str] = []
    downloaded: dict[str, Path] = {}
    cooldown = state.not_before
    pages = 0
    bodies = 0
    next_page: str | None = None
    partial = False
    evidence_by_url: dict[str, list[dict[str, str]]] = {}
    with closing(open_connection(directory / "receipts.sqlite")) as conn:

        def evidence_for(url: str) -> dict[str, Any] | None:
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

        def receive(
            url: str, kind: Literal["index", "document"]
        ) -> tuple[ArticleReceipt, bytes | None]:
            nonlocal cooldown
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
            for response in responses:
                receipt = response.receipt
                if receipt.status == "not_modified" and previous is not None:
                    if receipt.body_ref is None:
                        raise ValueError("304 lacks a concrete body reference")
                    # The exact original was validated before this conditional GET.
                    read_body(conn, receipt.body_ref)
                with conn:
                    retain_receipt(conn, receipt, body=response.body)
                receipt_ids.append(receipt.receipt_id)
                kinds[receipt.receipt_id] = kind
                if receipt.status == "ok":
                    # Updating an existing mapping moves it to the bounded recent end.
                    planning.pop(url, None)
                    planning[url] = {
                        "receipt": receipt.model_dump(mode="json"),
                        "stage": {"stage_id": stage_id},
                    }
                elif receipt.status != "not_modified":
                    failed.append(receipt.status)
                if receipt.cooldown_until is not None:
                    proposed = receipt.cooldown_until.isoformat()
                    if (
                        cooldown is None
                        or datetime.fromisoformat(cooldown) < receipt.cooldown_until
                    ):
                        cooldown = proposed
            final = responses[-1].receipt
            body = read_body(conn, final.body_ref) if final.body_ref is not None else None
            return final, body

        queue: list[str] = []
        active_url: str | None = None
        try:
            # Always overlap the front page, then continue the previous bounded page
            # window. It is not a claim to recover everything lost from a rolling feed.
            entry_urls = list(policy.entry_urls)
            page_url: str | None = entry_urls.pop(0)
            seen_pages = set()
            discovered_by_identity: dict[str, str] = {}
            while page_url and pages < policy.max_pages:
                if page_url in seen_pages:
                    break
                seen_pages.add(page_url)
                receipt, body = receive(page_url, "index")
                pages += 1
                if body is None:
                    partial = True
                    if receipt.cooldown_until is None and entry_urls:
                        page_url = entry_urls.pop(0)
                        continue
                    break
                try:
                    native = parse_index(
                        source, body, url=page_url, encoding=receipt.encoding or "utf-8"
                    )
                except ValueError:
                    if source != "nhk_news":
                        raise
                    # This feed's original is saved, but another independent
                    # NHK entry may still provide valid items in the same slot.
                    failed.append("index_parse_failed")
                    partial = True
                    page_url = entry_urls.pop(0) if entry_urls else None
                    continue
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
                resume_page = state.cursor.get("next_page") if pages == 1 else None
                page_url = entry_urls.pop(0) if entry_urls else resume_page or next_page
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
                list(dict.fromkeys([*known, *pending_urls, *fresh])) if policy.max_bodies else []
            )
            pending_urls = []
            seen_bodies: set[str] = set()
            while queue and bodies < policy.max_bodies:
                if cooldown and datetime.fromisoformat(cooldown) > datetime.now(UTC):
                    partial = True
                    break
                url = queue.pop(0)
                active_url = url
                if url in seen_bodies:
                    active_url = None
                    continue
                if (
                    urlsplit(url).hostname not in policy.allowed_hosts
                    or urlsplit(url).scheme != "https"
                ):
                    active_url = None
                    continue  # Alias/login/foreign-host identities do not expand crawling.
                seen_bodies.add(url)
                receipt, body = receive(url, "document")
                bodies += 1
                active_url = None
                link = evidence_for(url)
                if link is not None and attach_prior_index(link):
                    document_evidence[receipt.receipt_id] = {
                        field: link[field]
                        for field in ("index_receipt_id", "article_id", "native_locator", "title")
                    }
                elif source == "nhk_news":
                    # Its GUID may differ from the URL. Keep raw and mark the
                    # unresolved identity rather than inventing a URL identity.
                    failed.append("document_identity_unbound")
                if body is None:
                    pending_urls.append(url)
                elif source in {"boj_news", "mof_news"} and not body.startswith(b"%PDF-"):
                    # Only explicit attachments from the permitted document body are
                    # candidates; linked PDF GETs spend the same source body budget.
                    document = parse_document(
                        source, body, url=url, encoding=receipt.encoding or "utf-8"
                    )
                    attachments = document.metadata.get("attachments", [])
                    if isinstance(attachments, list):
                        queue[:0] = [
                            link
                            for link in attachments
                            if isinstance(link, str) and link not in queue
                        ]
                if receipt.cooldown_until is not None:
                    partial = True
                    break
            pending_urls.extend(queue)
        except Exception as error:
            # No source text/URL/error messages enter logs. Completed L1 remains
            # eligible for staging even if a later request or native parse fails.
            failed.append(type(error).__name__)
            pending_urls = list(
                dict.fromkeys([*pending_urls, *([active_url] if active_url else []), *queue])
            )
            partial = True
        # Keeping a finite cursor is honest about omitted backlog, not completeness.
        omitted_urls = max(0, len(pending_urls) - MAX_REQUEST_STATES)
        if omitted_urls:
            failed.append("pending_cursor_overflow")
        continuation_only = bool(pending_urls or next_page) and not (failed or partial)
        cursor = {
            "pending_urls": pending_urls[:MAX_REQUEST_STATES],
            "pending_evidence": {
                url: link
                for url in pending_urls[:MAX_REQUEST_STATES]
                if (link := evidence_for(url)) is not None
            },
            "next_page": next_page,
            "omitted_url_count": omitted_urls,
        }
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
            "status": "partial" if failed or partial or pending_urls or next_page else "completed",
            "continuation_only": continuation_only,
            "failures": sorted(set(failed)),
            "pages": pages,
            "bodies": bodies,
            "requests_used": budget.requests_used,
            "bytes_used": budget.bytes_used,
            "acquisition_seconds": round((datetime.now(UTC) - started).total_seconds(), 3),
            "omitted_url_count": omitted_urls,
        }
    write_json_atomic(directory / "result.json", report)
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
    return {
        **{
            key: value
            for key, value in report.items()
            if key not in {"projection_kinds", "receipt_ids", "document_evidence"}
        },
        "stage_id": saved_stage["stage_id"],
        "manifest_sha256": saved_stage["manifest_sha256"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=tuple(GROUPS), required=True)
    parser.add_argument("--created-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    reports = []
    group_budget = GroupBudget(GROUP_BYTES[args.group])
    store = ArticleObjectMetrics(
        Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
    )
    started = time.monotonic()
    with requests.Session() as session:
        for source in GROUPS[args.group]:
            try:
                report = acquire_source(
                    store,
                    source=source,
                    slot=slot_at(args.created_at),
                    identity=args.identity,
                    directory=args.directory / source,
                    session=session,
                    group_budget=group_budget,
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
