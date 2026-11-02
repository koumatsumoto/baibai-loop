"""Private durable originals for selected public-page acquisition attempts."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import uuid
import zlib
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from itertools import chain
from pathlib import Path
from types import TracebackType
from typing import Any

from baibai_engine.data_collection.receipts.public_pages.models import EquityWebSourceResponse
from baibai_engine.data_collection.receipts.public_pages.receipts import (
    validate_response as equity_web_validate_response,
)
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    LakePublishError,
    ObjectStore,
)
from baibai_jobs.acquisition.external_assessments.identity import (
    RAW_BUNDLE_FORMAT,
    RAW_PREFIX,
    RETAINED_RAW_PREFIX,
)
from baibai_jobs.acquisition.external_assessments.staging import prefix, read_plan, read_stages

RETENTION_DAYS = 3


def _date_prefix(day: date) -> str:
    return f"{RAW_PREFIX}utc-date/{day.isoformat()}"


_RECEIPT_FIELDS = {
    "receipt_id",
    "attempt_id",
    "source",
    "requested_ticker",
    "role",
    "parent_receipt_id",
    "requested_url",
    "received_at_utc",
    "selected_encoding",
    "http_status",
    "body_sha256",
    "body_bytes",
    "member",
}


def _validate_lineage(responses: list[EquityWebSourceResponse]) -> None:
    by_id: dict[str, EquityWebSourceResponse] = {}
    by_attempt: dict[str, tuple[str, str, set[str]]] = {}
    for response in responses:
        if response.receipt_id in by_id:
            raise ValueError("duplicate external assessment raw receipt")
        by_id[response.receipt_id] = response
        prior = by_attempt.get(response.attempt_id)
        if prior is None:
            by_attempt[response.attempt_id] = (
                response.source,
                response.requested_ticker,
                {response.role},
            )
        elif (response.source, response.requested_ticker) != prior[:2] or response.role in prior[2]:
            raise ValueError("invalid external assessment raw attempt identity")
        else:
            prior[2].add(response.role)
    for response in responses:
        if response.role == "iframe":
            assert response.parent_receipt_id is not None
            parent = by_id.get(response.parent_receipt_id)
            if (
                parent is None
                or parent.role != "page"
                or (parent.attempt_id, parent.source, parent.requested_ticker)
                != (response.attempt_id, response.source, response.requested_ticker)
            ):
                raise ValueError("external assessment iframe parent is not retained")


def _unique_json(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate external assessment raw field")
        result[key] = value
    return result


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
            f"{RETAINED_RAW_PREFIX}utc-date/{self.created_at_utc.date().isoformat()}/bundles/{self.run_id}"
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

    def append(self, received: EquityWebSourceResponse) -> None:
        equity_web_validate_response(received)
        for previous in self.receipts:
            if previous["receipt_id"] == received.receipt_id:
                raise ValueError("duplicate external assessment raw receipt")
            if previous["attempt_id"] == received.attempt_id and (
                (previous["source"], previous["requested_ticker"])
                != (
                    received.source,
                    received.requested_ticker,
                )
                or previous["role"] == received.role
            ):
                raise ValueError("invalid external assessment raw attempt identity")
        if received.role == "iframe" and not any(
            previous["receipt_id"] == received.parent_receipt_id
            and previous["attempt_id"] == received.attempt_id
            and previous["role"] == "page"
            for previous in self.receipts
        ):
            raise ValueError("external assessment iframe parent is not retained")
        member = f"bodies/{len(self.receipts):04d}.html"
        path = self.directory / member
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(received.body)
        path.chmod(0o600)
        # Only these fields enter the archive; no cookies, headers or credentials.
        self.receipts.append(
            {
                "receipt_id": received.receipt_id,
                "attempt_id": received.attempt_id,
                "source": received.source,
                "requested_ticker": received.requested_ticker,
                "role": received.role,
                "parent_receipt_id": received.parent_receipt_id,
                "requested_url": received.requested_url,
                "received_at_utc": received.received_at_utc.isoformat(timespec="microseconds"),
                "selected_encoding": received.selected_encoding,
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


def read_raw_bundle(
    store: ObjectStore, key: str
) -> tuple[dict[str, Any], tuple[EquityWebSourceResponse, ...]]:
    """Validate canonical v2 receipts and bytes without extracting paths to disk."""
    retained = retained_key(key)
    # 旧stageの参照文字列は保持し、同じbytesの保全copyを優先して読む。
    retained_object = store.head(retained)
    actual = retained if retained_object is not None else key
    if retained_object is None and (actual == retained or store.head(actual) is None):
        raise ValueError("external original is missing; keep this source incomplete")
    payload = store.get_bytes(actual)
    try:
        return _decode_raw_bundle(payload, key)
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, zlib.error) as error:
        raise ValueError("invalid external assessment raw container") from error


def retained_key(key: str) -> str:
    if key.startswith(RETAINED_RAW_PREFIX):
        return key
    if key.startswith(RAW_PREFIX):
        return RETAINED_RAW_PREFIX + key.removeprefix(RAW_PREFIX)
    raise ValueError("external original is outside retained raw scope")


def preserve_raw_bundle(store: ObjectStore, key: str) -> str:
    """旧3日prefixの原本を同一bytesで保全する。元key・stage・原IDは変更しない。"""
    target = retained_key(key)
    existing = store.head(target)
    if existing is not None:
        payload = store.get_bytes(target)
        if hashlib.sha256(payload).hexdigest() != existing.metadata.get("sha256"):
            raise ValueError("retained external original failed integrity")
        _decode_raw_bundle(payload, key)
        if (
            target != key
            and (original := store.head(key)) is not None
            and (
                original.size != len(payload)
                or original.metadata.get("sha256") != hashlib.sha256(payload).hexdigest()
            )
        ):
            raise ValueError("retained external original conflicts with legacy bytes")
        return target
    if target == key:
        raise ValueError("retained external original is missing")
    original = store.head(key)
    if original is None:
        raise ValueError("legacy external original is missing; do not recreate its reception")
    payload = store.get_bytes(key)
    digest = hashlib.sha256(payload).hexdigest()
    if len(payload) != original.size or original.metadata.get("sha256") != digest:
        raise ValueError("legacy external original failed integrity")
    _decode_raw_bundle(payload, key)
    with tempfile.TemporaryDirectory(prefix="baibai-retain-original-") as directory:
        path = Path(directory) / "original.tar.gz"
        path.write_bytes(payload)
        path.chmod(0o600)
        try:
            store.put_file(
                target,
                path,
                sha256=digest,
                content_md5=base64.b64encode(
                    hashlib.md5(payload, usedforsecurity=False).digest()
                ).decode(),
                content_type="application/gzip",
                if_none_match=True,
            )
        except Exception:
            # PUT結果不明なら同じ保存先を照合するだけ。上書きや新しい原本生成はしない。
            if store.head(target) is None or store.get_bytes(target) != payload:
                raise
    if store.get_bytes(target) != payload:
        raise ValueError("retained external original readback mismatch")
    return target


def _decode_raw_bundle(
    payload: bytes, key: str
) -> tuple[dict[str, Any], tuple[EquityWebSourceResponse, ...]]:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        manifest_stream = archive.extractfile("manifest.json")
        if manifest_stream is None:
            raise ValueError("missing external assessment raw manifest")
        with manifest_stream:
            manifest = json.load(manifest_stream, object_pairs_hook=_unique_json)
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"format", "batch_id", "cohort_index", "created_at_utc", "receipts"}
            or manifest["format"] != RAW_BUNDLE_FORMAT
            or not isinstance(manifest["batch_id"], str)
            or type(manifest["cohort_index"]) is not int
            or manifest["cohort_index"] < 0
            or not isinstance(manifest["created_at_utc"], str)
        ):
            raise ValueError("invalid external assessment raw bundle")
        prefix(manifest["batch_id"])
        try:
            created = datetime.fromisoformat(manifest["created_at_utc"])
        except ValueError:
            raise ValueError("invalid external assessment raw bundle time") from None
        if created.utcoffset() != timedelta(0) or not re.fullmatch(
            rf"(?:{re.escape(RAW_PREFIX)}|{re.escape(RETAINED_RAW_PREFIX)})"
            rf"utc-date/{created.date().isoformat()}/bundles/"
            rf"{re.escape(manifest['batch_id'])}/cohort-{manifest['cohort_index']:04d}-[0-9a-f]{{32}}\.tar\.gz",
            key,
        ):
            raise ValueError("external assessment raw bundle binding mismatch")
        receipts = manifest.get("receipts")
        if not isinstance(receipts, list) or not receipts:
            raise ValueError("invalid external assessment raw receipts")
        expected_members = ["manifest.json"]
        responses: list[EquityWebSourceResponse] = []
        for index, receipt in enumerate(receipts):
            member = f"bodies/{index:04d}.html"
            if (
                not isinstance(receipt, dict)
                or set(receipt) != _RECEIPT_FIELDS
                or receipt.get("member") != member
            ):
                raise ValueError("invalid external assessment raw member")
            info = archive.getmember(member)
            if not info.isfile():
                raise ValueError("external assessment raw body must be a regular file")
            stream = archive.extractfile(info)
            assert stream is not None
            with stream:
                body = stream.read()
            if (
                type(receipt["body_bytes"]) is not int
                or receipt["body_bytes"] < 0
                or not isinstance(receipt["body_sha256"], str)
            ):
                raise ValueError("invalid external assessment raw body metadata")
            if (
                len(body) != receipt["body_bytes"]
                or hashlib.sha256(body).hexdigest() != receipt["body_sha256"]
            ):
                raise LakePublishError("external assessment raw body readback mismatch")
            expected_members.append(member)
            try:
                received_at = datetime.fromisoformat(receipt["received_at_utc"])
            except (TypeError, ValueError):
                raise ValueError("invalid external assessment raw receipt time") from None
            response = EquityWebSourceResponse(
                receipt_id=receipt["receipt_id"],
                attempt_id=receipt["attempt_id"],
                source=receipt["source"],
                requested_ticker=receipt["requested_ticker"],
                role=receipt["role"],
                parent_receipt_id=receipt["parent_receipt_id"],
                requested_url=receipt["requested_url"],
                received_at_utc=received_at,
                selected_encoding=receipt["selected_encoding"],
                body=body,
            )
            equity_web_validate_response(response)
            if (
                receipt["received_at_utc"] != received_at.isoformat(timespec="microseconds")
                or receipt["http_status"] != 200
            ):
                raise ValueError("invalid external assessment raw receipt metadata")
            responses.append(response)
        if archive.getnames() != expected_members:
            raise ValueError("unexpected external assessment raw archive members")
    _validate_lineage(responses)
    return manifest, tuple(responses)


def retention_inventory(objects: Iterable[tuple[str, int]], *, as_of: date) -> dict[str, Any]:
    """Report age groups only; age does not prove a bundle is unreferenced or safe to delete."""
    keep_from = as_of - timedelta(days=RETENTION_DAYS - 1)
    groups: dict[str, dict[str, Any]] = {}
    unclassified = {"objects": 0, "bytes": 0}
    retained = {"objects": 0, "bytes": 0}
    for key, size in objects:
        if key.startswith(RETAINED_RAW_PREFIX):
            retained["objects"] += 1
            retained["bytes"] += size
            continue
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
        "retained": retained,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inventory or explicitly preserve exact external assessment originals"
    )
    parser.add_argument("--preserve-run-id", help="Copy one exact plan originals; never delete")
    parser.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(UTC).date())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or "baibai-stores")
        if args.preserve_run_id:
            plan = read_plan(store, args.preserve_run_id)
            if plan is None:
                raise ValueError("exact external plan is missing")
            preserved = []
            for index, (stage, _) in sorted(read_stages(store, plan).items()):
                for key in stage.raw_bundle_keys:
                    manifest, _ = read_raw_bundle(store, key)
                    if manifest["batch_id"] != plan.run_id or manifest["cohort_index"] != index:
                        raise ValueError("external preservation plan binding changed")
                    target = preserve_raw_bundle(store, key)
                    remote = store.head(target)
                    assert remote is not None
                    preserved.append(
                        {
                            "original_key": key,
                            "retained_key": target,
                            "bytes": remote.size,
                            "sha256": remote.metadata["sha256"],
                        }
                    )
            report = {
                "mode": "preserve_exact_plan",
                "run_id": plan.run_id,
                "plan_sha256": plan.plan_sha256,
                "preserved": preserved,
            }
        else:
            report = retention_inventory(
                chain(store.iter_objects(RAW_PREFIX), store.iter_objects(RETAINED_RAW_PREFIX)),
                as_of=args.as_of,
            )
        print(json.dumps(report, sort_keys=True))
        return 0
    except Exception as error:
        print(
            f"External Assessment raw inventory failed; type={type(error).__name__}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
