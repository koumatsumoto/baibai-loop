"""File identities are independent of byte content and transfer chunk boundaries."""

import hashlib

import pytest

from baibai_engine.storage.snapshots.sources import sha256_file


@pytest.mark.parametrize("payload", [b"", bytes(range(256)) * (32 * 1024) + b"tail"])
def test_snapshot_file_digest_covers_empty_and_multiple_chunks(tmp_path, payload):
    path = tmp_path / "source"
    path.write_bytes(payload)
    assert sha256_file(path) == hashlib.sha256(payload).hexdigest()
    assert path.read_bytes() == payload
