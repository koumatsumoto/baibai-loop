"""Immutable source receipt handoff in the existing private stores bucket."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from compression import zstd

from baibai_engine.data_collection.receipts.native_sources.planning_inputs import (
    validate_planning_input,
)
from baibai_engine.storage.objects.json import put_private, read_write_once_private
from baibai_engine.storage.objects.r2 import (
    LakeCASConflict,
    LakePublishError,
    ObjectStore,
    VerifiedObjectMetadataReader,
)

_PLANNING_INPUT_GROUPS = {
    "boj-acquisition-input-v1": "economic-boj",
    "pmi-acquisition-input-v1": "economic-spglobal_pmi",
    "umich-acquisition-input-v1": "economic-umich_sca",
    "ecb-fx-acquisition-input-v1": "economic-ecb_fx",
    "estat-acquisition-input-v1": "economic-estat",
    "jquants-acquisition-input-v1": "economic-jquants",
    "mof-jgb-acquisition-input-v1": "economic-mof_jgb",
    "frb-h15-acquisition-input-v1": "economic-frb_h15",
    "yahoo-acquisition-input-v1": "economic-yahoo",
    "multpl-acquisition-input-v1": "economic-multpl",
    "cftc-acquisition-input-v1": "economic-cftc",
    "nikkei-indexes-acquisition-input-v1": "economic-nikkei_indexes",
}
_PLANNING_INPUT_FORMATS = {group: wire for wire, group in _PLANNING_INPUT_GROUPS.items()}
_FRED_RECEIPT_FORMAT = "fred-acquisition-receipts-zstd-v1"
_FRED_STAGE_PREFIX = "fred_csv-"
_MAX_FRED_RECEIPT_BYTES = 128 * 1024**2
_FRED_DECODE_CHUNK = 1024**2
_FRED_MAX_WINDOW_LOG = 23  # 8 MiB
_FRED_MANIFEST_OVERHEAD_BYTES = 512
_SQLITE_HEADER = b"SQLite format 3\x00"


def stage_prefix(stage_id: str, *, market: bool = False, namespace: str | None = None) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", stage_id) is None:
        raise ValueError("invalid source handoff identity")
    namespace = namespace or ("market-sources" if market else "economic-sources")
    if namespace not in {"market-sources", "economic-sources", "source-inputs", "article-sources"}:
        raise ValueError("invalid source handoff namespace")
    return f"coordination/{namespace}/{stage_id}"


@dataclass(frozen=True)
class _ReceiptFile:
    path: Path
    sha256: str
    content_md5: str
    size: int

    @classmethod
    def measure(
        cls, path: Path, *, encode: Callable[[bytes], object] | None = None
    ) -> _ReceiptFile:
        sha, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024**2), b""):
                if encode is not None:
                    if size == 0 and not chunk.startswith(_SQLITE_HEADER):
                        raise ValueError("compressed FRED requires a closed SQLite receipt")
                    if size + len(chunk) > _MAX_FRED_RECEIPT_BYTES:
                        raise ValueError("compressed FRED receipt grew after staging began")
                    encode(chunk)
                size += len(chunk)
                sha.update(chunk)
                md5.update(chunk)
        return cls(path, sha.hexdigest(), base64.b64encode(md5.digest()).decode(), size)


def _fred_stage(
    stage_id: str, *, market: bool, namespace: str | None, input_group: str | None
) -> bool:
    return (
        not market
        and namespace in (None, "economic-sources")
        and input_group is None
        and stage_id.startswith(_FRED_STAGE_PREFIX)
        and stage_id != _FRED_STAGE_PREFIX
    )


def _measure_fred_storage(path: Path, wire_path: Path) -> tuple[_ReceiptFile, _ReceiptFile | None]:
    with zstd.open(
        wire_path,
        "xb",
        options={
            zstd.CompressionParameter.compression_level: 1,
            zstd.CompressionParameter.window_log: _FRED_MAX_WINDOW_LOG,
        },
    ) as encoder:
        raw = _ReceiptFile.measure(path, encode=encoder.write)
    if raw.size < 100:
        raise ValueError("compressed FRED requires a closed SQLite receipt")
    if wire_path.stat().st_size + _FRED_MANIFEST_OVERHEAD_BYTES >= raw.size:
        return raw, None
    return raw, _ReceiptFile.measure(wire_path)


def save_handoff(
    store: ObjectStore,
    stage_id: str,
    directory: Path,
    *,
    market: bool = False,
    namespace: str | None = None,
    reusable_receipt: dict[str, object] | None = None,
    input_group: str | None = None,
    current_input_stage: str | None = None,
    compress_fred: bool = False,
) -> str:
    prefix = stage_prefix(stage_id, market=market, namespace=namespace)
    path = directory / "receipts.sqlite"
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    fred = _fred_stage(stage_id, market=market, namespace=namespace, input_group=input_group)
    if compress_fred and (
        not fred or not isinstance(result, dict) or result.get("group") != "fred_csv"
    ):
        raise ValueError("compressed FRED requires its economic acquisition stage")

    with ExitStack() as stack:
        if compress_fred and path.stat().st_size <= _MAX_FRED_RECEIPT_BYTES:
            scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="fred-receipt-")))
            raw, wire = _measure_fred_storage(path, scratch / "receipts.sqlite.zst")
        else:
            raw, wire = _ReceiptFile.measure(path), None
        return _save_receipt(
            store,
            stage_id,
            prefix,
            result,
            raw=raw,
            wire=wire,
            fred=fred,
            market=market,
            namespace=namespace,
            reusable_receipt=reusable_receipt,
            input_group=input_group,
            current_input_stage=current_input_stage,
        )


def _save_receipt(
    store: ObjectStore,
    stage_id: str,
    prefix: str,
    result: object,
    *,
    raw: _ReceiptFile,
    wire: _ReceiptFile | None,
    fred: bool,
    market: bool,
    namespace: str | None,
    reusable_receipt: dict[str, object] | None,
    input_group: str | None,
    current_input_stage: str | None,
) -> str:
    receipt_format = (
        _PLANNING_INPUT_FORMATS.get(input_group or "") if namespace == "source-inputs" else None
    )
    receipt_field = "planning_key" if receipt_format else "receipt_key"
    if receipt_format:
        if market:
            raise ValueError("planning input is not a market receipt handoff")
        validate_planning_input(raw.path, _PLANNING_INPUT_GROUPS[receipt_format])
    raw_key = f"{prefix}/receipts-{raw.sha256}.sqlite"
    encoded_key = raw_key + ".zst"
    key = encoded_key if wire is not None else raw_key
    chosen = wire or raw
    if reusable_receipt is not None and reusable_receipt.get("receipt_sha256") == raw.sha256:
        if namespace != "source-inputs" or input_group is None:
            raise ValueError("receipt reuse is limited to scoped source inputs")
        reusable_receipt_key = reusable_receipt.get(receipt_field)
        pattern = (
            rf"coordination/source-inputs/{re.escape(input_group)}-"
            rf"[A-Za-z0-9_-]+/receipts-{raw.sha256}\.sqlite"
        )
        if (
            isinstance(reusable_receipt_key, str)
            and reusable_receipt.get("receipt_bytes") == raw.size
            and re.fullmatch(pattern, reusable_receipt_key) is not None
        ):
            key = reusable_receipt_key

    manifest_key = prefix + "/handoff.json"
    prior = read_write_once_private(store, manifest_key) if fred else None
    if fred and prior is None and store.head(raw_key if wire is not None else encoded_key):
        # An interrupted upload may not have a manifest yet. Retry its original format.
        raise LakePublishError("FRED receipt already staged in a different representation")
    remote = store.head(key)
    if key not in (raw_key, encoded_key) and (
        remote is None or remote.size != raw.size or remote.metadata.get("sha256") != raw.sha256
    ):
        # A retired receipt may be missing or invalid; publish this stage's own object.
        key = raw_key
        remote = store.head(key)
    value: dict[str, object] = {
        "result": result,
        receipt_field: key,
        "receipt_sha256": raw.sha256,
        "receipt_bytes": raw.size,
    }
    if wire is not None:
        value.update(
            receipt_format=_FRED_RECEIPT_FORMAT,
            stored_sha256=wire.sha256,
            stored_bytes=wire.size,
        )
    elif receipt_format:
        # Pre-reader-first code must fail rather than silently broaden recovery.
        value["receipt_format"] = receipt_format
    manifest = json.dumps(value, sort_keys=True).encode()
    if prior is not None and prior.payload != manifest:
        raise LakePublishError("source handoff identity already has different receipts")
    if remote is None:
        chosen.path.chmod(0o600)
        try:
            remote = store.put_file(
                key,
                chosen.path,
                sha256=chosen.sha256,
                content_md5=chosen.content_md5,
                content_type="application/zstd" if wire is not None else "application/vnd.sqlite3",
                if_none_match=True,
            )
        except LakeCASConflict:
            if not fred:
                raise
            remote = store.head(key)
    if (
        remote is None
        or remote.size != chosen.size
        or remote.metadata.get("sha256") != chosen.sha256
    ):
        raise LakePublishError("source private receipt write mismatch")
    if (
        current_input_stage is not None
        and reusable_receipt is not None
        and json.dumps({**reusable_receipt, "result": result}, sort_keys=True).encode() == manifest
    ):
        # The same complete manifest would be written with only a new issue time.
        return current_input_stage
    if not fred:
        prior = read_write_once_private(store, manifest_key)
    if prior is None:
        try:
            put_private(store, manifest_key, manifest, previous_etag=None)
        except LakeCASConflict:
            if not fred:
                raise
            prior = read_write_once_private(store, manifest_key)
            if prior is None:
                raise
    if prior is not None and prior.payload != manifest:
        raise LakePublishError("source handoff identity already has different receipts")
    return stage_id


def load_handoff(
    store: ObjectStore,
    stage_id: str,
    directory: Path,
    *,
    market: bool = False,
    namespace: str | None = None,
    input_group: str | None = None,
    expected_manifest_sha256: str | None = None,
) -> None:
    prefix = stage_prefix(stage_id, market=market, namespace=namespace)
    saved = read_write_once_private(store, prefix + "/handoff.json")
    if expected_manifest_sha256 is not None and (
        saved is None or hashlib.sha256(saved.payload).hexdigest() != expected_manifest_sha256
    ):
        raise LakePublishError("fixed source handoff manifest changed")
    if saved is None:
        raise LakePublishError("source private handoff is not complete")
    manifest = json.loads(saved.payload)
    receipt_format = manifest.get("receipt_format") if isinstance(manifest, dict) else None
    if receipt_format == _FRED_RECEIPT_FORMAT:
        if not _fred_stage(stage_id, market=market, namespace=namespace, input_group=input_group):
            raise ValueError("compressed FRED requires its economic acquisition stage")
        _load_fred_receipt(store, prefix, directory, manifest)
        return
    planning_group = (
        _PLANNING_INPUT_GROUPS.get(receipt_format) if isinstance(receipt_format, str) else None
    )
    if isinstance(manifest, dict) and "receipt_format" in manifest and planning_group is None:
        raise ValueError("invalid source handoff manifest: unsupported receipt format")
    if planning_group is not None:
        if (
            market
            or namespace != "source-inputs"
            or input_group != planning_group
            or "receipt_key" in manifest
        ):
            raise ValueError("planning input is not a source receipt handoff")
        manifest["receipt_key"] = manifest.get("planning_key")
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("receipt_sha256"), str)
        or type(manifest.get("receipt_bytes")) is not int
        or not isinstance(manifest.get("receipt_key"), str)
        or not isinstance(manifest.get("result"), dict)
    ):
        raise ValueError("invalid source handoff manifest")
    digest, size = manifest["receipt_sha256"], manifest["receipt_bytes"]
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise LakePublishError("source handoff receipt identity differs")
    key = f"{prefix}/receipts-{digest}.sqlite"
    if namespace == "source-inputs" and input_group is not None:
        pattern = (
            rf"coordination/source-inputs/{re.escape(input_group)}-"
            rf"[A-Za-z0-9_-]+/receipts-{digest}\.sqlite"
        )
        if re.fullmatch(pattern, manifest["receipt_key"]) is not None:
            key = manifest["receipt_key"]
    if manifest["receipt_key"] != key:
        raise LakePublishError("source handoff receipt identity differs")
    remote = store.head(key)
    if remote is None or remote.size != size or remote.metadata.get("sha256") != digest:
        raise LakePublishError("source handoff receipt metadata differs")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "receipts.sqlite"
    store.download_file(key, path, expect_bytes=size)
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != digest:
        raise LakePublishError("source handoff receipt content differs")
    if planning_group is not None:
        validate_planning_input(path, planning_group)
    path.chmod(0o600)
    (directory / "result.json").write_text(json.dumps(manifest["result"]), encoding="utf-8")


def _load_fred_receipt(
    store: ObjectStore, prefix: str, directory: Path, manifest: dict[str, object]
) -> None:
    result = manifest.get("result")
    if (
        not isinstance(result, dict)
        or result.get("group") != "fred_csv"
        or "planning_key" in manifest
    ):
        raise ValueError("compressed receipt requires the FRED acquisition group")

    raw_sha, raw_bytes = manifest.get("receipt_sha256"), manifest.get("receipt_bytes")
    stored_sha, stored_bytes = manifest.get("stored_sha256"), manifest.get("stored_bytes")
    if (
        not isinstance(raw_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", raw_sha) is None
        or not isinstance(stored_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", stored_sha) is None
        or type(raw_bytes) is not int
        or type(stored_bytes) is not int
        or not 0 < stored_bytes < raw_bytes <= _MAX_FRED_RECEIPT_BYTES
        or raw_bytes < 100
    ):
        raise ValueError("invalid compressed FRED receipt descriptor")
    key = f"{prefix}/receipts-{raw_sha}.sqlite.zst"
    if manifest.get("receipt_key") != key:
        raise LakePublishError("compressed FRED receipt identity differs")
    if directory.is_symlink() or (
        directory.exists() and (not directory.is_dir() or any(directory.iterdir()))
    ):
        raise FileExistsError("compressed FRED restore requires a new or empty packet directory")
    if not isinstance(store, VerifiedObjectMetadataReader):
        raise LakePublishError("compressed FRED receipt requires a bounded fixed object read")
    encoded = store.get_verified_object(key, sha256=stored_sha, max_bytes=stored_bytes)
    if encoded is None or encoded[1].size != stored_bytes:
        raise LakePublishError("compressed FRED receipt size differs")

    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory.parent, prefix=".fred-receipt-") as scratch:
        temporary = Path(scratch)
        raw = temporary / "receipts.sqlite"
        _restore_fred_receipt(encoded[0], raw, raw_bytes=raw_bytes, raw_sha=raw_sha)
        report = temporary / "result.json"
        report.write_text(json.dumps(result), encoding="utf-8")
        for path in (raw, report):
            path.chmod(0o600)
        # Publish both files together; a competing nonempty packet cannot be replaced.
        temporary.replace(directory)


def _restore_fred_receipt(encoded: bytes, path: Path, *, raw_bytes: int, raw_sha: str) -> None:
    if not encoded.startswith(b"\x28\xb5\x2f\xfd"):
        raise LakePublishError("invalid compressed FRED receipt frame")
    decoder = zstd.ZstdDecompressor(
        options={zstd.DecompressionParameter.window_log_max: _FRED_MAX_WINDOW_LOG}
    )
    payload = memoryview(encoded)
    digest = hashlib.sha256()
    offset = written = 0
    with path.open("xb") as output:
        while not decoder.eof:
            chunk: bytes | memoryview = b""
            if decoder.needs_input:
                chunk = payload[offset : offset + _FRED_DECODE_CHUNK]
                if not chunk:
                    raise LakePublishError("compressed FRED receipt frame is truncated")
                offset += len(chunk)
            try:
                decoded = decoder.decompress(
                    chunk, max_length=min(raw_bytes - written + 1, _FRED_DECODE_CHUNK)
                )
            except (EOFError, zstd.ZstdError) as error:
                raise LakePublishError("invalid compressed FRED receipt frame") from error
            written += len(decoded)
            if written > raw_bytes:
                raise LakePublishError("compressed FRED receipt exceeds its raw size")
            output.write(decoded)
            digest.update(decoded)
    if decoder.unused_data or offset != len(payload):
        raise LakePublishError("compressed FRED receipt contains trailing data")
    if written != raw_bytes or digest.hexdigest() != raw_sha:
        raise LakePublishError("compressed FRED receipt content differs")
    with path.open("rb") as stream:
        if stream.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
            raise LakePublishError("compressed FRED receipt does not restore SQLite")
