"""Private durable originals for selected public-page acquisition attempts."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import re
import tarfile
import tempfile
import uuid
import zlib
from datetime import UTC, datetime, timedelta
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
    ObjectMetadataReader,
    ObjectStore,
    R2Error,
)
from baibai_jobs.acquisition.external_assessments.identity import (
    RAW_BUNDLE_FORMAT,
    RAW_PREFIX,
    RETAINED_RAW_PREFIX,
)
from baibai_jobs.acquisition.external_assessments.staging import prefix

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


def _read_bundle_bytes(store: ObjectStore, key: str, *, require_http_404: bool) -> bytes | None:
    retained = retained_key(key)
    strict_r2 = store if require_http_404 and isinstance(store, Boto3R2Store) else None

    def present(target: str) -> bool:
        if strict_r2 is not None:
            return strict_r2.head(target, require_http_404=True) is not None
        return store.head(target) is not None

    if retained == key and isinstance(store, ObjectMetadataReader):
        saved = (
            strict_r2.get_bytes_with_metadata(key, require_http_404=True)
            if strict_r2 is not None
            else store.get_bytes_with_metadata(key)
        )
        if saved is not None:
            return saved[0]
        # A missing reference is source-local; contradictory/unavailable storage
        # must abort common integration. Do not retry GET or choose another key.
        if present(key):
            raise R2Error("retained external original GET missing despite present HEAD")
        return None
    # A legacy reference has two locations. Once HEAD chooses one, a failed
    # body read must not silently switch locations.
    if present(retained):
        return store.get_bytes(retained)
    if retained != key and present(key):
        return store.get_bytes(key)
    return None


def read_raw_bundle(
    store: ObjectStore, key: str, *, allow_missing: bool = False
) -> tuple[dict[str, Any], tuple[EquityWebSourceResponse, ...]] | None:
    """Validate exact v2 originals. Only confirmed absence may be returned as None."""
    payload = _read_bundle_bytes(store, key, require_http_404=allow_missing)
    if payload is None:
        if allow_missing:
            return None
        raise ValueError("external original is missing; keep this source incomplete")
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


def verify_retained_raw_bundle(store: ObjectStore, key: str) -> None:
    """参照が旧keyでも、retainedの同じ原本が既にあることだけを検証する。"""
    target = retained_key(key)
    existing = store.head(target)
    if existing is None:
        raise ValueError("retained external original is missing")
    payload = store.get_bytes(target)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != existing.metadata.get("sha256"):
        raise ValueError("retained external original failed integrity")
    _decode_raw_bundle(payload, key)
    if (
        target != key
        and (original := store.head(key)) is not None
        and (original.size != len(payload) or original.metadata.get("sha256") != digest)
    ):
        raise ValueError("retained external original conflicts with legacy bytes")


def _decode_raw_bundle(
    payload: bytes, key: str
) -> tuple[dict[str, Any], tuple[EquityWebSourceResponse, ...]]:
    with (
        gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as uncompressed,
        tarfile.open(fileobj=uncompressed, mode="r:") as archive,
    ):
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
        body_end = 0
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
            body_end = info.offset_data + info.size
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
        # The v2 writer emits one gzip member. After the last retained body
        # it pads the TAR block, writes two end blocks and pads one TAR record.
        # Read only that tail plus one byte so gzip checks the trailer/CRC
        # without expanding a concatenated member or unreferenced padding.
        end_blocks = body_end + (-body_end % tarfile.BLOCKSIZE) + 2 * tarfile.BLOCKSIZE
        tar_size = end_blocks + (-end_blocks % tarfile.RECORDSIZE)
        if int.from_bytes(payload[-4:], "little") != tar_size % (1 << 32):
            raise ValueError("invalid external assessment raw gzip extent")
        remaining = tar_size - body_end
        uncompressed.seek(body_end)
        tail = uncompressed.read(remaining + 1)
        if len(tail) != remaining or any(tail):
            raise ValueError("invalid external assessment raw tar tail")
    _validate_lineage(responses)
    return manifest, tuple(responses)
