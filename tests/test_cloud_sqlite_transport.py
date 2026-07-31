from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from tools.cloud.sqlite_transport import (
    TransportError,
    build_metadata,
    compare_heads,
    compress,
    compressed_identity,
    decompress,
    read_head,
    resolve,
)

RAW_TIME = "2026-07-31T00:00:00.123456Z"
RAW_REVISION = 1_785_456_000_123_456_000
RAW_ETAG = "0123456789abcdef0123456789abcdef"
SQLITE_SHA = hashlib.sha256(b"sqlite snapshot").hexdigest()


def _head_file(
    tmp_path: Path,
    name: str,
    *,
    modified: str = RAW_TIME,
    etag: str = RAW_ETAG,
    metadata: dict[str, str] | None = None,
) -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {
                "LastModified": modified,
                "ETag": f'"{etag}"',
                "ContentLength": 123,
                "Metadata": metadata or {},
            }
        ),
        encoding="utf-8",
    )
    return path


def _metadata(
    *,
    revision: int = RAW_REVISION,
    sha256: str = SQLITE_SHA,
    raw_revision: int | None = RAW_REVISION,
    raw_etag: str | None = RAW_ETAG,
) -> dict[str, str]:
    result = {
        "baibai-format": "sqlite-zstd-v1",
        "baibai-snapshot-revision": str(revision),
        "baibai-published-at": "2026-07-31T00:01:00Z",
        "baibai-sqlite-sha256": sha256,
        "baibai-uncompressed-size": str(len(b"sqlite snapshot")),
    }
    if raw_revision is not None:
        result["baibai-raw-revision-ns"] = str(raw_revision)
    if raw_etag is not None:
        result["baibai-raw-etag"] = raw_etag
    return result


def test_zstd_round_trip_preserves_sha256_and_size(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    compressed = tmp_path / "source.sqlite.zst"
    restored = tmp_path / "restored.sqlite"
    source.write_bytes((b"SQLite format 3\0" + bytes(range(256))) * 1024)

    sha256, size = compress(source, compressed, level=1)
    decompress(
        compressed,
        restored,
        expected_sha256=sha256,
        expected_size=size,
    )

    assert restored.read_bytes() == source.read_bytes()
    assert sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert size == source.stat().st_size


def test_decompress_removes_output_when_metadata_identity_is_wrong(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite"
    compressed = tmp_path / "source.sqlite.zst"
    restored = tmp_path / "restored.sqlite"
    source.write_bytes(b"sqlite snapshot")
    _, size = compress(source, compressed, level=1)

    with pytest.raises(TransportError, match="does not match metadata"):
        decompress(
            compressed,
            restored,
            expected_sha256="0" * 64,
            expected_size=size,
        )

    assert not restored.exists()


def test_resolve_accepts_raw_only_and_compressed_only(tmp_path: Path) -> None:
    raw = read_head(_head_file(tmp_path, "raw.json"))
    compressed = read_head(
        _head_file(
            tmp_path,
            "compressed.json",
            metadata=_metadata(raw_revision=None, raw_etag=None),
        )
    )

    assert resolve(raw, None) == ("raw", None)
    selection, identity = resolve(None, compressed)
    assert selection == "compressed"
    assert identity is not None
    assert identity.sha256 == SQLITE_SHA


def test_resolve_requires_hash_comparison_for_migration_baseline(
    tmp_path: Path,
) -> None:
    raw = read_head(_head_file(tmp_path, "raw.json"))
    compressed = read_head(_head_file(tmp_path, "compressed.json", metadata=_metadata()))

    selection, identity = resolve(raw, compressed)

    assert selection == "same"
    assert identity is not None
    assert identity.revision == RAW_REVISION


def test_resolve_prefers_a_later_compressed_revision_with_same_raw_anchor(
    tmp_path: Path,
) -> None:
    raw = read_head(_head_file(tmp_path, "raw.json"))
    compressed = read_head(
        _head_file(
            tmp_path,
            "compressed.json",
            metadata=_metadata(revision=RAW_REVISION + 1),
        )
    )

    assert resolve(raw, compressed)[0] == "compressed"


@pytest.mark.parametrize(
    ("modified", "etag"),
    [
        ("2026-07-31T00:00:01Z", RAW_ETAG),
        (RAW_TIME, "fedcba9876543210fedcba9876543210"),
    ],
)
def test_resolve_fails_closed_when_the_retained_raw_object_changes(
    tmp_path: Path, modified: str, etag: str
) -> None:
    raw = read_head(_head_file(tmp_path, "raw.json", modified=modified, etag=etag))
    compressed = read_head(
        _head_file(
            tmp_path,
            "compressed.json",
            metadata=_metadata(revision=RAW_REVISION + 1),
        )
    )

    with pytest.raises(TransportError, match="raw object changed"):
        resolve(raw, compressed)


def test_resolve_rejects_raw_appearing_after_compressed_only_seed(
    tmp_path: Path,
) -> None:
    raw = read_head(_head_file(tmp_path, "raw.json"))
    compressed = read_head(
        _head_file(
            tmp_path,
            "compressed.json",
            metadata=_metadata(raw_revision=None, raw_etag=None),
        )
    )

    with pytest.raises(TransportError, match="appeared"):
        resolve(raw, compressed)


def test_resolve_rejects_retained_raw_disappearing_after_migration(
    tmp_path: Path,
) -> None:
    compressed = read_head(
        _head_file(
            tmp_path,
            "compressed.json",
            metadata=_metadata(revision=RAW_REVISION + 1),
        )
    )

    with pytest.raises(TransportError, match="retained raw object disappeared"):
        resolve(None, compressed)


def test_metadata_inherits_raw_anchor_and_advances_revision(tmp_path: Path) -> None:
    raw = read_head(_head_file(tmp_path, "raw.json"))
    baseline_text = build_metadata(
        mode="baseline",
        sha256=SQLITE_SHA,
        size=len(b"sqlite snapshot"),
        raw_head=raw,
        previous_head=None,
    )
    baseline = dict(item.split("=", maxsplit=1) for item in baseline_text.split(","))
    previous = read_head(_head_file(tmp_path, "compressed.json", metadata=baseline))

    publish_text = build_metadata(
        mode="publish",
        sha256=SQLITE_SHA,
        size=len(b"sqlite snapshot"),
        raw_head=raw,
        previous_head=previous,
    )
    published = dict(item.split("=", maxsplit=1) for item in publish_text.split(","))

    assert baseline["baibai-snapshot-revision"] == str(RAW_REVISION)
    assert published["baibai-snapshot-revision"] == str(RAW_REVISION + 1)
    assert published["baibai-raw-revision-ns"] == str(RAW_REVISION)
    assert published["baibai-raw-etag"] == RAW_ETAG


def test_metadata_rejects_publish_after_retained_raw_disappears(
    tmp_path: Path,
) -> None:
    previous = read_head(
        _head_file(
            tmp_path,
            "compressed.json",
            metadata=_metadata(revision=RAW_REVISION + 1),
        )
    )

    with pytest.raises(TransportError, match="retained raw object disappeared"):
        build_metadata(
            mode="publish",
            sha256=SQLITE_SHA,
            size=len(b"sqlite snapshot"),
            raw_head=None,
            previous_head=previous,
        )


def test_compressed_metadata_rejects_partial_raw_anchor(tmp_path: Path) -> None:
    compressed = read_head(
        _head_file(
            tmp_path,
            "compressed.json",
            metadata=_metadata(raw_etag=None),
        )
    )

    with pytest.raises(TransportError, match="incomplete raw anchor"):
        compressed_identity(compressed)


def test_head_object_requires_an_etag(tmp_path: Path) -> None:
    path = tmp_path / "head.json"
    path.write_text(
        json.dumps(
            {
                "LastModified": RAW_TIME,
                "ContentLength": 123,
                "Metadata": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TransportError, match="ETag is missing"):
        read_head(path)


def test_compare_heads_detects_a_remote_change(tmp_path: Path) -> None:
    left = read_head(_head_file(tmp_path, "left.json"))
    right = read_head(
        _head_file(
            tmp_path,
            "right.json",
            etag="fedcba9876543210fedcba9876543210",
        )
    )

    with pytest.raises(TransportError, match="changed during transfer"):
        compare_heads(left, right)
