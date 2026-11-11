"""S3互換object転送。snapshotや候補政策を解釈せず、指定keyと条件だけを扱う。"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, Never, Protocol, runtime_checkable

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ConnectTimeoutError, ReadTimeoutError

from baibai_engine.storage.filesystem import fsync_directory

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import PutObjectRequestTypeDef

# Preserve the established per-attempt transfer budget and conditional-write errors.
_R2_ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")

_BASE_OPERATION_TIMEOUT_SECONDS = 120

_MIN_TRANSFER_BYTES_PER_SECOND = 4 * 1024 * 1024


class LakePublishError(RuntimeError):
    pass


class R2Error(LakePublishError):
    """R2 operation failed; source-contract errors keep the broader base type."""


class LakeCASConflict(LakePublishError):
    pass


def _retry_after(error: ClientError) -> str | int | None:
    headers = _response_metadata(error).get("HTTPHeaders")
    value = headers.get("retry-after") if isinstance(headers, Mapping) else None
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 128:
        return "invalid"
    if value.isascii() and value.isdecimal():
        return int(value)
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return "invalid"
        return parsed.astimezone(UTC).isoformat()
    except (TypeError, ValueError, OverflowError):
        return "invalid"


class _SerialRequests:
    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.lock = Lock()
        self.next_start = 0.0
        self.stopped = False

    def check(self) -> None:
        if self.stopped:
            raise R2Error("R2 serial request scope stopped after HTTP 429")

    @contextmanager
    def request(self, operation: str) -> Iterator[None]:
        with self.lock:
            self.check()
            delay = self.next_start - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            try:
                yield
            except ClientError as error:
                status = _response_metadata(error).get("HTTPStatusCode")
                if status in (429, "429") or _error_code(error) == "429":
                    self.stopped = True
                    logging.getLogger(__name__).warning(
                        "R2 serial request stopped: operation=%s status=%s retry_after=%s",
                        operation,
                        _http_status(error) or "unknown",
                        _retry_after(error),
                    )
                raise
            finally:
                # Completion-based spacing also covers client setup and streamed bodies.
                self.next_start = time.monotonic() + self.interval


@dataclass(frozen=True)
class RemoteObject:
    etag: str
    size: int
    metadata: Mapping[str, str]
    content_type: str


class ObjectStore(Protocol):
    def head(self, key: str) -> RemoteObject | None: ...

    def get_bytes(self, key: str) -> bytes: ...

    def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None: ...

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        content_md5: str,
        content_type: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> RemoteObject: ...


@runtime_checkable
class ObjectMetadataReader(Protocol):
    """Optional atomic GET response access; byte-only stores retain their read contract."""

    def get_bytes_with_metadata(self, key: str) -> tuple[bytes, RemoteObject] | None: ...


class Boto3R2Store:
    """S3-compatible R2 adapter with reusable in-process connections."""

    def __init__(
        self,
        *,
        bucket: str,
        env: Mapping[str, str] | None = None,
        verify_put_with_head: bool = True,
    ) -> None:
        source = dict(os.environ if env is None else env)
        account_id = _required(source, "R2_ACCOUNT_ID")
        if _R2_ACCOUNT_ID.fullmatch(account_id) is None:
            raise LakePublishError("R2_ACCOUNT_ID must be 32 lowercase hexadecimal characters")
        self.bucket = bucket
        self.endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        self.access_key_id = _required(source, "R2_ACCESS_KEY_ID")
        self.secret_access_key = _required(source, "R2_SECRET_ACCESS_KEY")
        self._clients: dict[tuple[int, int], S3Client] = {}
        self._serial: _SerialRequests | None = None
        self._scope_lock = Lock()
        self.verify_put_with_head = verify_put_with_head

    @contextmanager
    def serial_requests(self, *, min_interval_seconds: float) -> Iterator[None]:
        """Pace one joined group of small-object requests; never retry a 429.

        The caller must join its workers before leaving this store-wide scope.
        Unrelated work should use another store. The default adapter is unchanged.
        """
        if not math.isfinite(min_interval_seconds) or min_interval_seconds <= 0:
            raise ValueError("serial request interval must be positive and finite")
        state = _SerialRequests(min_interval_seconds)
        with self._scope_lock:
            if self._serial is not None:
                raise ValueError("serial request scopes cannot overlap")
            self._serial = state
        try:
            yield
            state.check()
        finally:
            with self._scope_lock:
                self._serial = None

    @contextmanager
    def _request(self, operation: str) -> Iterator[None]:
        state = self._serial
        if state is None:
            yield
        else:
            with state.request(operation):
                yield

    def _client(self, payload_bytes: int | None = None) -> S3Client:
        timeout = _operation_timeout(payload_bytes)
        attempts = 1 if self._serial is not None else 3
        cache_key = (timeout, attempts)
        cached = self._clients.get(cache_key)
        if cached is not None:
            return cached
        # A store can be owned by a prefetch worker; never share boto3's default
        # session while separate workers construct their clients.
        client = boto3.session.Session().client(
            "s3",
            endpoint_url=self.endpoint,
            region_name="auto",
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            config=Config(
                connect_timeout=_BASE_OPERATION_TIMEOUT_SECONDS,
                read_timeout=timeout,
                retries={"mode": "standard", "total_max_attempts": attempts},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        self._clients[cache_key] = client
        return client

    def close(self) -> None:
        """Release the connections owned by this store."""
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def head(self, key: str) -> RemoteObject | None:
        try:
            with self._request("head-object"):
                value = self._client().head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                return None
            _raise_r2_error("head-object", exc, key=key)
        except (ConnectTimeoutError, ReadTimeoutError) as exc:
            _log_r2_failure("head-object", exc, key=key)
            raise R2Error("R2 head-object timed out") from exc
        except BotoCoreError as exc:
            _log_r2_failure("head-object", exc, key=key)
            raise R2Error("R2 head-object failed") from exc
        return _remote_object(value)

    def iter_objects(self, prefix: str) -> Iterator[tuple[str, int]]:
        """List keys and sizes under one prefix without downloading object bodies."""
        if self._serial is not None:
            raise ValueError("serial request scope supports small objects, not listing")
        try:
            for page in (
                self._client()
                .get_paginator("list_objects_v2")
                .paginate(Bucket=self.bucket, Prefix=prefix)
            ):
                for item in page.get("Contents", []):
                    yield item["Key"], item["Size"]
        except ClientError as exc:
            _raise_r2_error("list-objects", exc)
        except BotoCoreError as exc:
            _log_r2_failure("list-objects", exc)
            raise R2Error("R2 list-objects failed") from exc

    def get_bytes(self, key: str) -> bytes:
        try:
            with self._request("get-object"):
                response = self._client().get_object(Bucket=self.bucket, Key=key)
                body = response["Body"]
                try:
                    return body.read()
                finally:
                    body.close()
        except (ConnectTimeoutError, ReadTimeoutError) as exc:
            _log_r2_failure("get-object", exc, key=key)
            raise R2Error("R2 get-object timed out") from exc
        except ClientError as exc:
            _raise_r2_error("get-object", exc, key=key)
        except BotoCoreError as exc:
            _log_r2_failure("get-object", exc, key=key)
            raise R2Error("R2 get-object failed") from exc

    def get_bytes_with_metadata(self, key: str) -> tuple[bytes, RemoteObject] | None:
        """Return the body and metadata from the same GET, without an extra HEAD."""
        try:
            with self._request("get-object"):
                response = self._client().get_object(Bucket=self.bucket, Key=key)
                body = response["Body"]
                try:
                    return body.read(), _remote_object(response)
                finally:
                    body.close()
        except (ConnectTimeoutError, ReadTimeoutError) as exc:
            _log_r2_failure("get-object", exc, key=key)
            raise R2Error("R2 get-object timed out") from exc
        except ClientError as exc:
            if _error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                return None
            _raise_r2_error("get-object", exc, key=key)
        except BotoCoreError as exc:
            _log_r2_failure("get-object", exc, key=key)
            raise R2Error("R2 get-object failed") from exc

    def download_file(
        self,
        key: str,
        path: Path,
        *,
        expect_bytes: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".part",
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            with self._request("get-object"):
                response = self._client(expect_bytes).get_object(Bucket=self.bucket, Key=key)
                body = response["Body"]
                try:
                    written = 0
                    with temporary_path.open("wb") as target:
                        while chunk := body.read(8 * 1024 * 1024):
                            written += len(chunk)
                            if max_bytes is not None and written > max_bytes:
                                raise R2Error(f"R2 get-object exceeds size limit: {max_bytes}")
                            target.write(chunk)
                        target.flush()
                        os.fsync(target.fileno())
                finally:
                    body.close()
            if expect_bytes is not None and written != expect_bytes:
                raise R2Error(
                    f"R2 get-object size mismatch: expected {expect_bytes}, got {written}"
                )
            temporary_path.replace(path)
            fsync_directory(path.parent)
        except (ConnectTimeoutError, ReadTimeoutError) as exc:
            _log_r2_failure("get-object", exc, key=key)
            raise R2Error("R2 get-object timed out") from exc
        except ClientError as exc:
            _raise_r2_error("get-object", exc, key=key)
        except BotoCoreError as exc:
            _log_r2_failure("get-object", exc, key=key)
            raise R2Error("R2 get-object failed") from exc
        finally:
            temporary_path.unlink(missing_ok=True)

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        content_md5: str,
        content_type: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> RemoteObject:
        request: PutObjectRequestTypeDef = {
            "Bucket": self.bucket,
            "Key": key,
            "ContentType": content_type,
            "Metadata": {
                "sha256": sha256,
                "content-md5": content_md5,
                "integrity": "content-md5-v1",
            },
            "ContentMD5": content_md5,
        }
        if if_match is not None:
            request["IfMatch"] = if_match
        if if_none_match:
            request["IfNoneMatch"] = "*"
        try:
            with self._request("put-object"), path.open("rb") as body:
                request["Body"] = body
                response = self._client(path.stat().st_size).put_object(**request)
        except (ConnectTimeoutError, ReadTimeoutError) as exc:
            _log_r2_failure("put-object", exc, key=key)
            raise R2Error("R2 put-object timed out") from exc
        except ClientError as exc:
            _raise_r2_error("put-object", exc, key=key)
        except BotoCoreError as exc:
            _log_r2_failure("put-object", exc, key=key)
            raise R2Error("R2 put-object failed") from exc
        if not self.verify_put_with_head:
            # Private acquisition uses the acknowledged conditional PUT generation.
            # Content-MD5 verifies the submitted bytes; metadata is part of that PUT.
            # An absent acknowledgement must stop, never be repaired by guessing.
            etag = response.get("ETag", "").strip('"')
            if not etag:
                raise R2Error("R2 put-object acknowledgement lacks a generation")
            return RemoteObject(
                etag=etag,
                size=path.stat().st_size,
                metadata=request["Metadata"],
                content_type=content_type,
            )
        result = self.head(key)
        if result is None:
            raise R2Error("R2 object missing after successful put")
        return result


def _remote_object(value: Mapping[str, Any]) -> RemoteObject:
    return RemoteObject(
        etag=str(value["ETag"]).strip('"'),
        size=int(value["ContentLength"]),
        metadata={str(k): str(v) for k, v in value.get("Metadata", {}).items()},
        content_type=str(value.get("ContentType", "application/octet-stream")),
    )


def _error_code(error: ClientError) -> str:
    details = error.response.get("Error")
    code = details.get("Code") if isinstance(details, Mapping) else None
    return str(code) if isinstance(code, (str, int)) else ""


def _response_metadata(error: ClientError) -> Mapping[str, Any]:
    metadata = error.response.get("ResponseMetadata")
    return metadata if isinstance(metadata, Mapping) else {}


def _http_status(error: ClientError) -> int | None:
    # Error.Code is a service code, not evidence of the HTTP response status.
    status = _response_metadata(error).get("HTTPStatusCode")
    if isinstance(status, str) and re.fullmatch(r"[1-5][0-9]{2}", status):
        return int(status)
    return status if type(status) is int and 100 <= status <= 599 else None


def _log_r2_failure(
    operation: str, error: ClientError | BotoCoreError, *, key: str | None = None
) -> None:
    status = _http_status(error) if isinstance(error, ClientError) else None
    # Never attach the exception: its message/cause can contain private request data.
    logging.getLogger(__name__).warning(
        "R2 request failed: operation=%s status=%s key_sha256=%s",
        operation,
        status if status is not None else "unknown",
        hashlib.sha256(key.encode("utf-8")).hexdigest() if key is not None else "none",
    )


def _raise_r2_error(operation: str, error: ClientError, *, key: str | None = None) -> Never:
    _log_r2_failure(operation, error, key=key)
    code = _error_code(error)
    status = _response_metadata(error).get("HTTPStatusCode")
    if code in {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"} or status in (
        409,
        412,
    ):
        raise LakeCASConflict(f"R2 conditional write conflict: {operation}") from error
    raise R2Error(f"R2 {operation} failed") from error


def _operation_timeout(payload_bytes: int | None) -> int:
    """Leave time for control-plane latency and a conservative object transfer.

    Botocore applies this budget to each attempt, not the whole retry sequence.
    """
    if payload_bytes is None or payload_bytes <= 0:
        return _BASE_OPERATION_TIMEOUT_SECONDS
    transfer = -(-payload_bytes // _MIN_TRANSFER_BYTES_PER_SECOND)
    return _BASE_OPERATION_TIMEOUT_SECONDS + transfer


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise LakePublishError(f"required environment variable is missing: {name}")
    return value
