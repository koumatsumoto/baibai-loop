"""Prepare an exact article-only recovery plan without fetching a live provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.articles.native import discovery_revision_for
from baibai_engine.data_collection.receipts.articles.contracts import digest_json
from baibai_engine.data_collection.receipts.articles.store import (
    copy_receipt_closure,
    read_body,
    read_receipt,
)
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError, ObjectStore
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.article_discovery import DISCOVERY_SOURCES, DiscoveryWork
from baibai_jobs.acquisition.article_planning import (
    ARTICLE_NAMESPACE,
    ARTICLE_SOURCES,
    commit_stage,
    load_discovery_cursor,
    load_planning,
    slot_at,
)
from baibai_jobs.acquisition.economic_staging import stage_prefix
from baibai_jobs.acquisition.external_assessments.staging import read_private
from baibai_jobs.acquisition.receipts import download_fixed


def exact_entry(
    store: ObjectStore, *, source: str, stage_id: str, manifest_sha256: str
) -> dict[str, Any]:
    if source not in ARTICLE_SOURCES or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
        raise ValueError("article recovery requires an article source and exact manifest digest")
    key = stage_prefix(stage_id, namespace=ARTICLE_NAMESPACE) + "/handoff.json"
    saved = read_private(store, key)
    if saved is None or hashlib.sha256(saved.payload).hexdigest() != manifest_sha256:
        raise LakePublishError("exact article recovery manifest differs or is missing")
    value = json.loads(saved.payload)["result"]
    if value["source"] != source:
        raise ValueError("article recovery source differs from immutable stage")
    clock = datetime.fromisoformat(value["started_at"])
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("article recovery requires the original timezone-aware clock")
    date.fromisoformat(value["asof"])
    return {
        "source": source,
        "market": True,
        "namespace": ARTICLE_NAMESPACE,
        "stage_id": stage_id,
        "manifest_sha256": manifest_sha256,
        "started_at": value["started_at"],
        "asof": value["asof"],
        "explicit_article_recovery": True,
    }


def prepare(
    store: ObjectStore, directory: Path, *, source: str, stage_id: str, manifest_sha256: str
) -> None:
    if (directory / "plan.json").exists():
        raise FileExistsError("recovery plan is already fixed")
    entry = exact_entry(store, source=source, stage_id=stage_id, manifest_sha256=manifest_sha256)
    directory.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        directory / "plan.json", {"handoffs": [entry], "external": [], "external_failed": []}
    )


def register_discovery(
    store: ObjectStore,
    directory: Path,
    *,
    source: str,
    stage_id: str,
    manifest_sha256: str,
    receipt_ids: list[str],
) -> dict[str, Any]:
    """Register selected retained originals, preserving live clocks and validators."""
    if source not in DISCOVERY_SOURCES or not 1 <= len(set(receipt_ids)) == len(receipt_ids) <= 16:
        raise ValueError("discovery recovery requires one source and 1–16 distinct receipts")
    revision = discovery_revision_for(source)
    request_key = digest_json([source, stage_id, manifest_sha256, sorted(receipt_ids), revision])
    state, etag = load_planning(store, source)
    cursor = load_discovery_cursor(store, state)
    work = DiscoveryWork(cursor)
    if request_key in work.recoveries:
        return {"status": "already_registered", "registered": 0}
    entry = exact_entry(store, source=source, stage_id=stage_id, manifest_sha256=manifest_sha256)
    original_dir = directory / "original"
    download_fixed(store, entry, original_dir)
    original_report = json.loads((original_dir / "result.json").read_text())
    selected = set(receipt_ids)
    if not selected <= set(original_report["receipt_ids"]) or any(
        original_report["projection_kinds"].get(identity) != "document" for identity in selected
    ):
        raise ValueError("recovery receipt is not an exact staged document")
    recovery_id = f"{source}-discovery-recovery-{uuid.uuid4().hex}"
    output = directory / recovery_id
    output.mkdir(parents=True, exist_ok=False)
    kinds: dict[str, str] = {}
    evidence: dict[str, dict[str, str]] = {}
    with (
        closing(connect_read_only(original_dir / "receipts.sqlite")) as raw,
        closing(open_connection(output / "receipts.sqlite")) as retained,
        retained,
    ):
        for identity in sorted(selected):
            receipt = read_receipt(raw, identity)
            if receipt.source != source or receipt.body_ref is None:
                raise ValueError("recovery receipt has no exact source original")
            read_body(raw, receipt.body_ref)
            copy_receipt_closure(raw, retained, identity)
            kinds[identity] = "document"
            link = original_report.get("document_evidence", {}).get(identity)
            if link is not None:
                index_id = link["index_receipt_id"]
                if original_report["projection_kinds"].get(index_id) != "index":
                    raise ValueError("recovery document index is not in its exact stage")
                copy_receipt_closure(raw, retained, index_id)
                kinds[index_id] = "index"
                evidence[identity] = link
            work.register(
                receipt,
                {"stage_id": recovery_id},
                {**link, "stage": {"stage_id": recovery_id}} if link is not None else None,
            )
    work.recoveries.add(request_key)
    now = datetime.now(UTC)
    write_json_atomic(
        output / "result.json",
        {
            "source": source,
            "operation": "register_discovery",
            "started_at": now.isoformat(),
            "asof": entry["asof"],
            "receipt_ids": sorted(kinds),
            "projection_kinds": kinds,
            "document_evidence": evidence,
            "status": "partial",
            "failures": ["discovery_pending"],
            "requests_used": 0,
            "bytes_used": 0,
            "pages": 0,
            "bodies": 0,
            "recovery_origin": entry,
            "recovery_request_key": request_key,
        },
    )
    saved = commit_stage(
        store,
        state=state,
        expected_etag=etag,
        slot=datetime.fromisoformat(state.last_slot) if state.last_slot else slot_at(now),
        stage_id=recovery_id,
        directory=output,
        requests=state.requests,
        cursor=work.wire(cursor),
        not_before=state.not_before,
        advance_slot=False,
    )
    return {"status": "registered", "registered": len(selected), "stage": saved}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "register-discovery"))
    parser.add_argument("--source", choices=ARTICLE_SOURCES, required=True)
    parser.add_argument("--stage-id", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--receipt-id", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        arguments = dict(
            source=args.source,
            stage_id=args.stage_id,
            manifest_sha256=args.manifest_sha256,
        )
        if args.command == "prepare":
            prepare(store, args.directory, **arguments)
        else:
            result = register_discovery(
                store, args.directory, receipt_ids=args.receipt_id, **arguments
            )
            print(json.dumps(result, sort_keys=True))
    except Exception as error:
        print(f"article recovery failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
