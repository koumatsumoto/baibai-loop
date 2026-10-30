"""Finite per-source article acquisition; each source saves partial L1 independently."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
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
    copy_receipt_closure,
    read_body,
    retain_receipt,
)
from baibai_engine.data_collection.transport.articles import ArticleBudget, fetch_article
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, ObjectStore
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.article_planning import (
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


def acquire_source(
    store: ObjectStore,
    *,
    source: str,
    slot: datetime,
    identity: str,
    directory: Path,
    session: requests.Session,
) -> dict[str, Any]:
    started = datetime.now(UTC)
    state, etag = load_planning(store, source)
    if not is_due(state, slot=slot, now=started):
        return {"source": source, "status": "not_due"}
    policy = SOURCES[source]
    stage_id = f"{source}-{slot:%Y%m%dT%H%M}-{identity}"
    directory.mkdir(parents=True, exist_ok=False)
    budget = ArticleBudget.start(
        max_requests=2 * (policy.max_pages + policy.max_bodies),
        max_bytes=policy.max_bytes,
        seconds=policy.max_seconds,
    )
    planning = dict(state.requests)
    pending_urls = list(state.cursor.get("pending_urls", []))
    receipt_ids: list[str] = []
    kinds: dict[str, str] = {}
    failed: list[str] = []
    downloaded: dict[str, Path] = {}
    cooldown = state.not_before
    pages = 0
    bodies = 0
    next_page: str | None = None
    partial = False
    last_get = 0.0
    with closing(open_connection(directory / "receipts.sqlite")) as conn:

        def receive(url: str, kind: str) -> tuple[ArticleReceipt, bytes | None]:
            nonlocal cooldown, last_get
            # One source uses <= 0.2 new request calls/sec. Retry/304 fallback is
            # separately bounded by ArticleBudget; no transport retry loop here.
            delay = 5.0 - (time.monotonic() - last_get)
            if delay > 0:
                time.sleep(min(delay, max(0.0, budget.deadline - time.monotonic())))
            budget.check()
            previous = planning.get(url)
            baseline = ArticleReceipt.model_validate(previous["receipt"]) if previous else None
            responses = fetch_article(
                session,
                source=source,
                url=url,
                allowed_hosts=policy.allowed_hosts,
                budget=budget,
                max_response_bytes=policy.max_response_bytes,
                baseline=baseline,
            )
            last_get = time.monotonic()
            for response in responses:
                receipt = response.receipt
                if receipt.status == "not_modified" and previous is not None:
                    if receipt.body_ref is None:
                        raise ValueError("304 lacks a concrete body reference")
                    entry = previous["stage"]
                    key = entry["stage_id"]
                    if key not in downloaded:
                        packet = directory / f"baseline-{len(downloaded)}"
                        download_fixed(store, entry, packet)
                        downloaded[key] = packet / "receipts.sqlite"
                    with closing(connect_read_only(downloaded[key])) as old, conn:
                        copy_receipt_closure(old, conn, receipt.body_ref.receipt_id)
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
            page_url = policy.entry_url
            seen_pages = set()
            discovered: list[str] = []
            while page_url and pages < policy.max_pages:
                if page_url in seen_pages:
                    break
                seen_pages.add(page_url)
                receipt, body = receive(page_url, "index")
                pages += 1
                if body is None:
                    partial = True
                    break
                native = parse_index(
                    source, body, url=page_url, encoding=receipt.encoding or "utf-8"
                )
                discovered.extend(
                    str(item.metadata.get("fetch_url") or item.url) for item in native
                )
                next_page = next_index_url(
                    source, body, url=page_url, encoding=receipt.encoding or "utf-8"
                )
                resume_page = state.cursor.get("next_page") if pages == 1 else None
                page_url = resume_page or next_page
            fresh = [url for url in discovered if url not in planning]
            # Two recent known URLs are revalidated even without sitemap lastmod.
            known = [url for url in discovered if url in planning][:2]
            queue = (
                list(dict.fromkeys([*pending_urls, *fresh, *known])) if policy.max_bodies else []
            )
            pending_urls = []
            while queue and bodies < policy.max_bodies:
                if cooldown and datetime.fromisoformat(cooldown) > datetime.now(UTC):
                    partial = True
                    break
                url = queue.pop(0)
                active_url = url
                if (
                    urlsplit(url).hostname not in policy.allowed_hosts
                    or urlsplit(url).scheme != "https"
                ):
                    continue  # Alias/login/foreign-host identities do not expand crawling.
                receipt, body = receive(url, "document")
                bodies += 1
                active_url = None
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
        cursor = {
            "pending_urls": pending_urls[:MAX_REQUEST_STATES],
            "next_page": next_page,
            "omitted_url_count": omitted_urls,
        }
        # Current index and recent bodies only; old raw history stays in immutable stages.
        planning = dict(list(planning.items())[-MAX_REQUEST_STATES:])
        report = {
            "source": source,
            "asof": slot.date().isoformat(),
            "started_at": started.isoformat(),
            "slot": slot.isoformat(),
            "receipt_ids": receipt_ids,
            "projection_kinds": kinds,
            "status": "partial" if failed or partial or pending_urls or next_page else "completed",
            "failures": sorted(set(failed)),
            "pages": pages,
            "bodies": bodies,
            "requests_used": budget.requests_used,
            "bytes_used": budget.bytes_used,
            "acquisition_seconds": round((datetime.now(UTC) - started).total_seconds(), 3),
            "omitted_url_count": omitted_urls,
        }
    write_json_atomic(directory / "result.json", report)
    commit_stage(
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
        key: value
        for key, value in report.items()
        if key not in {"projection_kinds", "receipt_ids"}
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=tuple(GROUPS), required=True)
    parser.add_argument("--created-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    reports = []
    store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
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
                )
            except Exception as error:
                report = {"source": source, "status": "failed", "reason": type(error).__name__}
            reports.append(report)
    print(json.dumps({"sources": reports}, sort_keys=True))
    return int(any(item["status"] in {"failed", "partial"} for item in reports))


if __name__ == "__main__":
    raise SystemExit(main())
