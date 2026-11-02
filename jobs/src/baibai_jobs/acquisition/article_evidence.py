"""Private exact original reads before integration; no provider GET or production write.

The immutable stage is provenance, not a lake release. The exported receipt ID,
receipt hash and concrete body reference survive subsequent daily integration.
Keep the private packet when citing it; a stage locator alone is not a retention guarantee.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.receipts.articles.store import read_body, read_receipt
from baibai_engine.storage.json import write_json_atomic
from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError, ObjectStore
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_jobs.acquisition.article_planning import ARTICLE_NAMESPACE, ARTICLE_SOURCES
from baibai_jobs.acquisition.economic_staging import stage_prefix
from baibai_jobs.acquisition.external_assessments.staging import read_private
from baibai_jobs.acquisition.receipts import download_fixed


def exact_entry(
    store: ObjectStore, *, source: str, stage_id: str, manifest_sha256: str
) -> dict[str, Any]:
    if source not in ARTICLE_SOURCES or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
        raise ValueError("article evidence requires an article source and exact manifest digest")
    key = stage_prefix(stage_id, namespace=ARTICLE_NAMESPACE) + "/handoff.json"
    saved = read_private(store, key)
    if saved is None or hashlib.sha256(saved.payload).hexdigest() != manifest_sha256:
        raise LakePublishError("exact article evidence manifest differs or is missing")
    value = json.loads(saved.payload)["result"]
    if value["source"] != source:
        raise ValueError("article evidence source differs from immutable stage")
    clock = datetime.fromisoformat(value["started_at"])
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("article evidence requires the original timezone-aware clock")
    date.fromisoformat(value["asof"])
    return {
        "source": source,
        "market": True,
        "namespace": ARTICLE_NAMESPACE,
        "stage_id": stage_id,
        "manifest_sha256": manifest_sha256,
        "started_at": value["started_at"],
        "asof": value["asof"],
    }


def export_evidence(
    store: ObjectStore,
    directory: Path,
    *,
    source: str,
    stage_id: str,
    manifest_sha256: str,
    receipt_digests: dict[str, str],
) -> dict[str, Any]:
    """Verify an immutable packet and named receipts before exporting private bytes."""
    if not 1 <= len(receipt_digests) <= 16 or any(
        not identity or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for identity, digest in receipt_digests.items()
    ):
        raise ValueError("exact evidence requires 1–16 receipt IDs and hashes")
    entry = exact_entry(store, source=source, stage_id=stage_id, manifest_sha256=manifest_sha256)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    packet = directory / "original"
    download_fixed(store, entry, packet)
    report = json.loads((packet / "result.json").read_text())
    if not receipt_digests.keys() <= set(report["receipt_ids"]):
        raise ValueError("requested receipt is outside the exact stage")
    outputs = []
    with closing(connect_read_only(packet / "receipts.sqlite")) as conn:
        for index, (identity, digest) in enumerate(receipt_digests.items()):
            receipt = read_receipt(conn, identity, receipt_sha256=digest)
            if receipt.source != source:
                raise ValueError("requested receipt belongs to another source")
            body = read_body(conn, receipt.body_ref) if receipt.body_ref is not None else None
            filename = f"body-{index}.bin" if body is not None else None
            if body is not None and filename is not None:
                with (directory / filename).open("xb") as output:
                    output.write(body)
            outputs.append(
                {
                    "receipt": receipt.model_dump(mode="json"),
                    "receipt_sha256": receipt.sha256,
                    "body_file": filename,
                    "projection_kind": report["projection_kinds"][identity],
                }
            )
    evidence = {"stage": entry, "receipts": outputs, "scope": "native_originals_only"}
    write_json_atomic(directory / "evidence.json", evidence)
    return {"stage_id": stage_id, "receipts": len(outputs), "output_dir": str(directory)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=ARTICLE_SOURCES, required=True)
    parser.add_argument("--stage-id", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument(
        "--receipt", nargs=2, action="append", required=True, metavar=("ID", "SHA256")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if len(dict(args.receipt)) != len(args.receipt):
            raise ValueError("duplicate receipt request")
        result = export_evidence(
            Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores"),
            args.directory,
            source=args.source,
            stage_id=args.stage_id,
            manifest_sha256=args.manifest_sha256,
            receipt_digests=dict(args.receipt),
        )
        print(json.dumps(result, sort_keys=True))
    except Exception as error:
        print(f"article evidence read failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
