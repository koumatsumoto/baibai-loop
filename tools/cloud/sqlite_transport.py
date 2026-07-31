#!/usr/bin/env python3
"""Validate and encode the compressed R2 transport for SQLite snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from compression import zstd

FORMAT = "sqlite-zstd-v1"
BUFFER_SIZE = 1024 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
ETAG_PATTERN = re.compile(r"[A-Za-z0-9-]{1,128}")


class TransportError(ValueError):
    """The transport object or its metadata violates the compatibility contract."""


@dataclass(frozen=True)
class ObjectHead:
    last_modified_ns: int
    etag: str
    content_length: int
    metadata: dict[str, str]


@dataclass(frozen=True)
class CompressedIdentity:
    revision: int
    published_at: str
    sha256: str
    uncompressed_size: int
    raw_revision: int | None
    raw_etag: str | None


def _integer(value: object, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise TransportError(f"{field} must be an integer")
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise TransportError(f"{field} must be an integer") from exc
    if result < minimum:
        raise TransportError(f"{field} must be >= {minimum}")
    return result


def _etag(value: object) -> str:
    if not isinstance(value, str):
        raise TransportError("ETag is missing")
    result = value.strip().strip('"')
    if not ETAG_PATTERN.fullmatch(result):
        raise TransportError("ETag has an unsupported format")
    return result


def _datetime_ns(value: object, *, field: str) -> int:
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TransportError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise TransportError(f"{field} must include a timezone")
    parsed = parsed.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = parsed - epoch
    return (
        delta.days * 86_400_000_000_000 + delta.seconds * 1_000_000_000 + delta.microseconds * 1_000
    )


def read_head(path: Path) -> ObjectHead:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransportError(f"invalid HeadObject response: {path}") from exc
    if not isinstance(payload, dict):
        raise TransportError("HeadObject response must be an object")
    metadata_value = payload.get("Metadata", {})
    if not isinstance(metadata_value, dict):
        raise TransportError("HeadObject Metadata must be an object")
    metadata = {str(key).lower(): str(value) for key, value in metadata_value.items()}
    return ObjectHead(
        last_modified_ns=_datetime_ns(payload.get("LastModified"), field="LastModified"),
        etag=_etag(payload.get("ETag")),
        content_length=_integer(payload.get("ContentLength"), field="ContentLength", minimum=0),
        metadata=metadata,
    )


def compressed_identity(head: ObjectHead) -> CompressedIdentity:
    metadata = head.metadata
    if metadata.get("baibai-format") != FORMAT:
        raise TransportError(f"compressed object must declare {FORMAT}")
    sha256 = metadata.get("baibai-sqlite-sha256", "")
    if not SHA256_PATTERN.fullmatch(sha256):
        raise TransportError("compressed object has an invalid SQLite SHA-256")
    published_at = metadata.get("baibai-published-at", "")
    _datetime_ns(published_at, field="baibai-published-at")
    raw_revision_value = metadata.get("baibai-raw-revision-ns")
    raw_etag_value = metadata.get("baibai-raw-etag")
    if (raw_revision_value is None) != (raw_etag_value is None):
        raise TransportError("compressed object has an incomplete raw anchor")
    raw_revision = (
        None
        if raw_revision_value is None
        else _integer(raw_revision_value, field="baibai-raw-revision-ns")
    )
    raw_etag = None if raw_etag_value is None else _etag(raw_etag_value)
    return CompressedIdentity(
        revision=_integer(
            metadata.get("baibai-snapshot-revision"),
            field="baibai-snapshot-revision",
        ),
        published_at=published_at,
        sha256=sha256,
        uncompressed_size=_integer(
            metadata.get("baibai-uncompressed-size"),
            field="baibai-uncompressed-size",
        ),
        raw_revision=raw_revision,
        raw_etag=raw_etag,
    )


def _digest_stream(stream: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(BUFFER_SIZE):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def digest_file(path: Path) -> tuple[str, int]:
    if not path.is_file():
        raise TransportError(f"input is not a file: {path}")
    with path.open("rb") as stream:
        return _digest_stream(stream)


def _verify_zstd(path: Path) -> tuple[str, int]:
    try:
        with zstd.open(path, "rb") as stream:
            return _digest_stream(stream)
    except (OSError, zstd.ZstdError) as exc:
        raise TransportError(f"invalid zstd frame: {path}") from exc


def compress(source: Path, output: Path, *, level: int) -> tuple[str, int]:
    if not source.is_file():
        raise TransportError(f"input is not a file: {source}")
    if output.exists():
        raise TransportError(f"refusing to overwrite transport output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with (
            source.open("rb") as source_stream,
            zstd.open(output, "wb", level=level) as output_stream,
        ):
            while chunk := source_stream.read(BUFFER_SIZE):
                output_stream.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        source_identity = (digest.hexdigest(), size)
        if _verify_zstd(output) != source_identity:
            raise TransportError("zstd round-trip changed the SQLite snapshot")
        return source_identity
    except Exception:
        output.unlink(missing_ok=True)
        raise


def decompress(
    source: Path,
    output: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> None:
    if not source.is_file():
        raise TransportError(f"input is not a file: {source}")
    if output.exists():
        raise TransportError(f"refusing to overwrite SQLite output: {output}")
    if not SHA256_PATTERN.fullmatch(expected_sha256):
        raise TransportError("expected SHA-256 is invalid")
    output.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with zstd.open(source, "rb") as source_stream, output.open("xb") as output_stream:
            while chunk := source_stream.read(BUFFER_SIZE):
                output_stream.write(chunk)
                digest.update(chunk)
                size += len(chunk)
                if size > expected_size:
                    raise TransportError("decompressed SQLite is larger than its metadata")
        if size != expected_size or digest.hexdigest() != expected_sha256:
            raise TransportError("decompressed SQLite identity does not match metadata")
    except Exception:
        output.unlink(missing_ok=True)
        raise


def resolve(
    raw_head: ObjectHead | None, compressed_head: ObjectHead | None
) -> tuple[str, CompressedIdentity | None]:
    if raw_head is None and compressed_head is None:
        raise TransportError("neither raw nor compressed store object exists")
    if compressed_head is None:
        return "raw", None
    identity = compressed_identity(compressed_head)
    if raw_head is None:
        if identity.raw_revision is not None or identity.raw_etag is not None:
            raise TransportError("retained raw object disappeared after migration")
        return "compressed", identity
    if identity.raw_revision is None or identity.raw_etag is None:
        raise TransportError(
            "raw object appeared after a compressed-only seed; refusing ambiguous data"
        )
    if raw_head.last_modified_ns != identity.raw_revision or raw_head.etag != identity.raw_etag:
        raise TransportError(
            "raw object changed after compressed transport migration; refusing stale data"
        )
    if identity.revision < identity.raw_revision:
        raise TransportError("compressed snapshot revision precedes its raw anchor")
    if identity.revision == identity.raw_revision:
        return "same", identity
    return "compressed", identity


def _published_at() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def build_metadata(
    *,
    mode: str,
    sha256: str,
    size: int,
    raw_head: ObjectHead | None,
    previous_head: ObjectHead | None,
) -> str:
    if not SHA256_PATTERN.fullmatch(sha256):
        raise TransportError("SQLite SHA-256 is invalid")
    if size < 0:
        raise TransportError("SQLite size must be non-negative")
    raw_revision: int | None = None
    raw_etag: str | None = None
    if mode == "baseline":
        if raw_head is None or previous_head is not None:
            raise TransportError("baseline metadata requires only a raw HeadObject")
        revision = raw_head.last_modified_ns
        raw_revision = revision
        raw_etag = raw_head.etag
    elif mode == "publish":
        if previous_head is None:
            if raw_head is not None:
                raise TransportError("raw migration baseline must be published first")
            revision = time.time_ns()
        else:
            previous = compressed_identity(previous_head)
            revision = previous.revision + 1
            raw_revision = previous.raw_revision
            raw_etag = previous.raw_etag
            if raw_head is None and raw_revision is not None:
                raise TransportError("retained raw object disappeared before publish")
            if raw_head is not None and (
                raw_revision != raw_head.last_modified_ns or raw_etag != raw_head.etag
            ):
                raise TransportError("raw object changed before compressed publish")
    else:
        raise TransportError(f"unknown metadata mode: {mode}")
    values = {
        "baibai-format": FORMAT,
        "baibai-snapshot-revision": str(revision),
        "baibai-published-at": _published_at(),
        "baibai-sqlite-sha256": sha256,
        "baibai-uncompressed-size": str(size),
    }
    if raw_revision is not None and raw_etag is not None:
        values["baibai-raw-revision-ns"] = str(raw_revision)
        values["baibai-raw-etag"] = raw_etag
    return ",".join(f"{key}={value}" for key, value in values.items())


def compare_heads(left: ObjectHead, right: ObjectHead) -> None:
    if left != right:
        raise TransportError("remote object changed during transfer")


def _optional_head(value: str | None) -> ObjectHead | None:
    return None if value is None else read_head(Path(value))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    digest_parser = subparsers.add_parser("digest")
    digest_parser.add_argument("--path", type=Path, required=True)

    compress_parser = subparsers.add_parser("compress")
    compress_parser.add_argument("--source", type=Path, required=True)
    compress_parser.add_argument("--output", type=Path, required=True)
    compress_parser.add_argument("--level", type=int, choices=(1, 3), default=1)

    decompress_parser = subparsers.add_parser("decompress")
    decompress_parser.add_argument("--source", type=Path, required=True)
    decompress_parser.add_argument("--output", type=Path, required=True)
    decompress_parser.add_argument("--sha256", required=True)
    decompress_parser.add_argument("--size", type=int, required=True)

    resolve_parser = subparsers.add_parser("resolve")
    resolve_parser.add_argument("--raw-head")
    resolve_parser.add_argument("--compressed-head")

    identity_parser = subparsers.add_parser("identity")
    identity_parser.add_argument("--compressed-head", type=Path, required=True)

    etag_parser = subparsers.add_parser("head-etag")
    etag_parser.add_argument("--head", type=Path, required=True)

    metadata_parser = subparsers.add_parser("metadata")
    metadata_parser.add_argument("--mode", choices=("baseline", "publish"), required=True)
    metadata_parser.add_argument("--sha256", required=True)
    metadata_parser.add_argument("--size", type=int, required=True)
    metadata_parser.add_argument("--raw-head")
    metadata_parser.add_argument("--previous-head")

    compare_parser = subparsers.add_parser("compare-heads")
    compare_parser.add_argument("--left", type=Path, required=True)
    compare_parser.add_argument("--right", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "digest":
            sha256, size = digest_file(args.path)
            print(sha256, size)
        elif args.command == "compress":
            sha256, size = compress(args.source, args.output, level=args.level)
            print(sha256, size)
        elif args.command == "decompress":
            decompress(
                args.source,
                args.output,
                expected_sha256=args.sha256,
                expected_size=args.size,
            )
        elif args.command == "resolve":
            selection, identity = resolve(
                _optional_head(args.raw_head), _optional_head(args.compressed_head)
            )
            if identity is None:
                print(selection)
            else:
                print(
                    selection,
                    identity.sha256,
                    identity.uncompressed_size,
                    identity.revision,
                )
        elif args.command == "identity":
            identity = compressed_identity(read_head(args.compressed_head))
            print(identity.sha256, identity.uncompressed_size, identity.revision)
        elif args.command == "head-etag":
            print(read_head(args.head).etag)
        elif args.command == "metadata":
            print(
                build_metadata(
                    mode=args.mode,
                    sha256=args.sha256,
                    size=args.size,
                    raw_head=_optional_head(args.raw_head),
                    previous_head=_optional_head(args.previous_head),
                )
            )
        elif args.command == "compare-heads":
            compare_heads(read_head(args.left), read_head(args.right))
        else:  # pragma: no cover - argparse owns the command choices
            raise AssertionError(args.command)
    except (OSError, TransportError, zstd.ZstdError) as exc:
        print(f"transport error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
