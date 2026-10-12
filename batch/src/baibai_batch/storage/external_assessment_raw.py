"""Temporary private assessment HTML; typed observations remain authoritative."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from baibai_batch.storage.external_assessment_identity import RAW_PREFIX, RECEIPT_FORMAT
from baibai_batch.storage.external_assessment_staging import prefix, put_private, read_private
from baibai_batch.storage.lake_publish import Boto3R2Store, LakePublishError, ObjectStore
from baibai_engine.batch_api import ExternalAssessmentReceivedHTML, ExternalAssessmentSource

RETENTION_DAYS = 3


def _received_date(received: datetime) -> date:
    if received.utcoffset() is None:
        raise ValueError("external assessment raw receipt requires a timezone-aware time")
    return received.astimezone(UTC).date()


def _date_prefix(day: date) -> str:
    return f"{RAW_PREFIX}utc-date/{day.isoformat()}"


def _blob_key(received: datetime, digest: str) -> str:
    return f"{_date_prefix(_received_date(received))}/html/sha256/{digest}.html.gz"


def read_raw_html(store: ObjectStore, receipt_key: str) -> tuple[dict[str, Any], bytes]:
    """Read a private receipt and recover the exact received bytes, before decoding."""
    saved = read_private(store, receipt_key)
    if saved is None:
        raise LakePublishError("external assessment raw receipt is missing")
    receipt = json.loads(saved.payload)
    if receipt.get("format") != RECEIPT_FORMAT:
        raise ValueError("invalid external assessment raw receipt")
    digest = receipt["body_sha256"]
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("invalid external assessment raw body hash")
    key = _blob_key(datetime.fromisoformat(receipt["fetched_at_utc"]), digest)
    if receipt["blob_key"] != key:
        raise ValueError("external assessment raw blob reference changed")
    body = gzip.decompress(store.get_bytes(key))
    if len(body) != receipt["body_bytes"] or hashlib.sha256(body).hexdigest() != digest:
        raise LakePublishError("external assessment raw body readback mismatch")
    return receipt, body


def save_raw_html(
    store: ObjectStore,
    *,
    run_id: str,
    source: ExternalAssessmentSource,
    ticker: str,
    request_number: int,
    received: ExternalAssessmentReceivedHTML,
) -> str:
    """Save a received public HTTP 200 body, after the typed outcome checkpoint."""
    prefix(run_id)
    if (
        source not in {"monex", "rakuten", "kabumap"}
        or re.fullmatch(r"[0-9A-Z]{4}", ticker) is None
    ):
        raise ValueError("invalid external assessment raw request identity")
    if type(request_number) is not int or request_number <= 0:
        raise ValueError("invalid external assessment raw request number")
    digest = hashlib.sha256(received.body).hexdigest()
    key = _blob_key(received.fetched_at_utc, digest)
    compressed = gzip.compress(received.body, mtime=0)
    compressed_sha = hashlib.sha256(compressed).hexdigest()
    remote = store.head(key)
    if remote is None:
        with tempfile.TemporaryDirectory(prefix="baibai-external-assessment-raw-") as temporary:
            path = Path(temporary) / "body.html.gz"
            path.write_bytes(compressed)
            path.chmod(0o600)
            remote = store.put_file(
                key,
                path,
                sha256=compressed_sha,
                content_md5=base64.b64encode(
                    hashlib.md5(compressed, usedforsecurity=False).digest()
                ).decode(),
                content_type="application/gzip",
                if_none_match=True,
            )
    if (
        not remote.etag
        or remote.size != len(compressed)
        or remote.metadata.get("sha256") != compressed_sha
    ):
        raise LakePublishError("external assessment raw blob receipt mismatch")
    # A receipt contains only the allowlisted fields below, never cookies/headers.
    receipt = {
        "format": RECEIPT_FORMAT,
        "batch_id": run_id,
        "distributor": source,
        "requested_ticker": ticker,
        "request_number": request_number,
        "url": received.url,
        "fetched_at_utc": received.fetched_at_utc.isoformat(timespec="microseconds"),
        "encoding": received.encoding,
        "http_status": 200,
        "body_sha256": digest,
        "body_bytes": len(received.body),
        "blob_key": key,
    }
    receipt_key = (
        f"{_date_prefix(_received_date(received.fetched_at_utc))}/receipts"
        f"/{run_id}/{source}/{ticker}/{request_number}.json"
    )
    payload = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    existing = store.head(receipt_key)
    if existing is None:
        put_private(store, receipt_key, payload, previous_etag=None)
    elif (
        not existing.etag
        or existing.size != len(payload)
        or existing.metadata.get("sha256") != hashlib.sha256(payload).hexdigest()
    ):
        raise LakePublishError("conflicting immutable external assessment raw receipt")
    return receipt_key


def retention_inventory(objects: Iterable[tuple[str, int]], *, as_of: date) -> dict[str, Any]:
    """Report date groups for review only; unclassified keys are not deletion candidates."""
    keep_from = as_of - timedelta(days=RETENTION_DAYS - 1)
    groups: dict[str, dict[str, Any]] = {}
    unclassified = {"objects": 0, "bytes": 0}
    for key, size in objects:
        matched = re.fullmatch(
            rf"{re.escape(RAW_PREFIX)}utc-date/(\d{{4}}-\d{{2}}-\d{{2}})/(html/sha256|receipts)/.+",
            key,
        )
        if matched is None:
            unclassified["objects"] += 1
            unclassified["bytes"] += size
            continue
        day = date.fromisoformat(matched[1])
        group = groups.setdefault(
            day.isoformat(),
            {
                "status": "delete_candidate" if day < keep_from else "keep",
                "objects": 0,
                "bytes": 0,
                "prefix": _date_prefix(day) + "/",
            },
        )
        group["objects"] += 1
        group["bytes"] += size
    return {
        "mode": "read_only_inventory",
        "as_of_utc_date": as_of.isoformat(),
        "keep_from_utc_date": keep_from.isoformat(),
        "retention_utc_dates": RETENTION_DAYS,
        "dates": dict(sorted(groups.items())),
        "unclassified": unclassified,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only inventory of diagnostic external assessment raw"
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(UTC).date())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        objects = store.iter_objects(RAW_PREFIX)
        print(json.dumps(retention_inventory(objects, as_of=args.as_of), sort_keys=True))
        return 0
    except Exception as error:
        print(
            f"External Assessment raw inventory failed; type={type(error).__name__}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
