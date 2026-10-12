"""Private public-page bodies for retail reparsing; separate from disposable checkpoints."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from baibai_batch.storage.lake_publish import LakePublishError, ObjectStore
from baibai_batch.storage.retail_staging import prefix, put_private, read_private
from baibai_engine.batch_api import RetailReceivedHTML, RetailSource

RECEIPT_FORMAT = "retail-public-html-receipt-v1"
BLOB_PREFIX = "retail-dynamics/raw-html/sha256/"


def read_raw_html(store: ObjectStore, receipt_key: str) -> tuple[dict[str, Any], bytes]:
    """Read a private receipt and recover the exact received bytes, before decoding."""
    saved = read_private(store, receipt_key)
    if saved is None:
        raise LakePublishError("retail raw receipt is missing")
    receipt = json.loads(saved.payload)
    if receipt.get("format") != RECEIPT_FORMAT:
        raise ValueError("invalid retail raw receipt")
    digest = receipt["body_sha256"]
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("invalid retail raw body hash")
    key = BLOB_PREFIX + digest + ".html.gz"
    if receipt["blob_key"] != key:
        raise ValueError("retail raw blob reference changed")
    body = gzip.decompress(store.get_bytes(key))
    if len(body) != receipt["body_bytes"] or hashlib.sha256(body).hexdigest() != digest:
        raise LakePublishError("retail raw body readback mismatch")
    return receipt, body


def save_raw_html(
    store: ObjectStore,
    *,
    run_id: str,
    source: RetailSource,
    ticker: str,
    request_number: int,
    received: RetailReceivedHTML,
) -> str:
    """Called only by the existing collector's public HTTP 200 body callback."""
    private_prefix = prefix(run_id)
    if source not in {"monex", "rakuten"} or re.fullmatch(r"[0-9A-Z]{4}", ticker) is None:
        raise ValueError("invalid retail raw request identity")
    if type(request_number) is not int or request_number <= 0:
        raise ValueError("invalid retail raw request number")
    digest = hashlib.sha256(received.body).hexdigest()
    key = BLOB_PREFIX + digest + ".html.gz"
    compressed = gzip.compress(received.body, mtime=0)
    compressed_sha = hashlib.sha256(compressed).hexdigest()
    remote = store.head(key)
    if remote is None:
        with tempfile.TemporaryDirectory(prefix="baibai-retail-raw-") as temporary:
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
        raise LakePublishError("retail raw blob receipt mismatch")
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
        private_prefix.replace("coordination/retail-dynamics/", "retail-dynamics/raw-receipts/", 1)
        + f"/{source}/{ticker}/{request_number}.json"
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
        raise LakePublishError("conflicting immutable retail raw receipt")
    return receipt_key
