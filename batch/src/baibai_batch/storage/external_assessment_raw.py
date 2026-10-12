"""Private diagnostic cohort bundles; typed stage outcomes remain authoritative."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any

from baibai_batch.storage.external_assessment_identity import RAW_BUNDLE_FORMAT, RAW_PREFIX
from baibai_batch.storage.external_assessment_staging import prefix
from baibai_batch.storage.lake_publish import Boto3R2Store, LakePublishError, ObjectStore
from baibai_engine.batch_api import ExternalAssessmentReceivedHTML, ExternalAssessmentSource

RETENTION_DAYS = 3


def _date_prefix(day: date) -> str:
    return f"{RAW_PREFIX}utc-date/{day.isoformat()}"


class RawBundle:
    """Spool public HTTP 200 bodies outside pair deadlines, then make one standard tar.gz."""

    def __init__(self, run_id: str, cohort_index: int) -> None:
        prefix(run_id)
        self.run_id = run_id
        self.cohort_index = cohort_index
        self._temporary = tempfile.TemporaryDirectory(prefix="baibai-assessment-raw-")
        self.directory = Path(self._temporary.name)
        self.receipts: list[dict[str, Any]] = []
        self.path: Path | None = None
        self.created_at_utc = datetime.now(UTC)
        self.key = (
            f"{_date_prefix(self.created_at_utc.date())}/bundles/{self.run_id}"
            f"/cohort-{self.cohort_index:04d}-{uuid.uuid4().hex}.tar.gz"
        )

    def __enter__(self) -> RawBundle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._temporary.cleanup()

    def append(
        self,
        source: ExternalAssessmentSource,
        ticker: str,
        received: ExternalAssessmentReceivedHTML,
    ) -> None:
        member = f"bodies/{len(self.receipts):04d}.html"
        path = self.directory / member
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(received.body)
        path.chmod(0o600)
        # Only these fields enter the archive; no cookies, headers or credentials.
        self.receipts.append(
            {
                "distributor": source,
                "requested_ticker": ticker,
                "url": received.url,
                "fetched_at_utc": received.fetched_at_utc.isoformat(timespec="microseconds"),
                "encoding": received.encoding,
                "http_status": 200,
                "body_sha256": hashlib.sha256(received.body).hexdigest(),
                "body_bytes": len(received.body),
                "member": member,
            }
        )

    def finish(self) -> str | None:
        if not self.receipts:
            return None
        manifest = {
            "format": RAW_BUNDLE_FORMAT,
            "batch_id": self.run_id,
            "cohort_index": self.cohort_index,
            "created_at_utc": self.created_at_utc.isoformat(),
            "receipts": self.receipts,
        }
        manifest_path = self.directory / "manifest.json"
        manifest_path.write_bytes(json.dumps(manifest, sort_keys=True).encode())
        manifest_path.chmod(0o600)
        self.path = self.directory / "cohort.tar.gz"
        with tarfile.open(self.path, "w:gz") as archive:
            archive.add(manifest_path, arcname="manifest.json", recursive=False)
            for receipt in self.receipts:
                archive.add(
                    self.directory / receipt["member"], arcname=receipt["member"], recursive=False
                )
        self.path.chmod(0o600)
        return self.key

    def upload(self, store: ObjectStore) -> None:
        if self.path is None:
            raise ValueError("raw bundle must be finalized before upload")
        sha = hashlib.sha256()
        md5 = hashlib.md5(usedforsecurity=False)
        with self.path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                sha.update(chunk)
                md5.update(chunk)
        remote = store.put_file(
            self.key,
            self.path,
            sha256=sha.hexdigest(),
            content_md5=base64.b64encode(md5.digest()).decode(),
            content_type="application/gzip",
            if_none_match=True,
        )
        if (
            not remote.etag
            or remote.size != self.path.stat().st_size
            or remote.metadata.get("sha256") != sha.hexdigest()
        ):
            raise LakePublishError("external assessment raw bundle write receipt mismatch")


def read_raw_bundle(store: ObjectStore, key: str) -> tuple[dict[str, Any], tuple[bytes, ...]]:
    """Restore exact bytes from a private tar.gz without extracting paths to disk."""
    payload = store.get_bytes(key)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        manifest_stream = archive.extractfile("manifest.json")
        if manifest_stream is None:
            raise ValueError("missing external assessment raw manifest")
        with manifest_stream:
            manifest = json.load(manifest_stream)
        if not isinstance(manifest, dict) or manifest.get("format") != RAW_BUNDLE_FORMAT:
            raise ValueError("invalid external assessment raw bundle")
        receipts = manifest.get("receipts")
        if not isinstance(receipts, list):
            raise ValueError("invalid external assessment raw receipts")
        expected_members = ["manifest.json"]
        bodies = []
        for index, receipt in enumerate(receipts):
            member = f"bodies/{index:04d}.html"
            if not isinstance(receipt, dict) or receipt.get("member") != member:
                raise ValueError("invalid external assessment raw member")
            info = archive.getmember(member)
            if not info.isfile():
                raise ValueError("external assessment raw body must be a regular file")
            stream = archive.extractfile(info)
            assert stream is not None
            with stream:
                body = stream.read()
            if (
                len(body) != receipt["body_bytes"]
                or hashlib.sha256(body).hexdigest() != receipt["body_sha256"]
            ):
                raise LakePublishError("external assessment raw body readback mismatch")
            expected_members.append(member)
            bodies.append(body)
        if archive.getnames() != expected_members:
            raise ValueError("unexpected external assessment raw archive members")
    return manifest, tuple(bodies)


def retention_inventory(objects: Iterable[tuple[str, int]], *, as_of: date) -> dict[str, Any]:
    """Report date groups for review only; unclassified keys are not deletion candidates."""
    keep_from = as_of - timedelta(days=RETENTION_DAYS - 1)
    groups: dict[str, dict[str, Any]] = {}
    unclassified = {"objects": 0, "bytes": 0}
    for key, size in objects:
        matched = re.fullmatch(
            rf"{re.escape(RAW_PREFIX)}utc-date/(\d{{4}}-\d{{2}}-\d{{2}})/(html/sha256|receipts|bundles)/.+",
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
