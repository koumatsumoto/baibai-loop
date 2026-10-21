from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from baibai_engine.storage.observations import lake_origin
from baibai_engine.storage.observations.lake_origin import LakeStoreOrigin, LakeStoreOriginError


@pytest.mark.parametrize("operation", ["read", "advance", "conflict"])
def test_origin_operations_close_their_owned_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    path = tmp_path / "market.sqlite"
    original = LakeStoreOrigin("release-original", "a" * 64)
    target = LakeStoreOrigin("release-next", "b" * 64)
    with closing(sqlite3.connect(path)) as setup:
        setup.execute(
            "CREATE TABLE lake_store_origin("
            "singleton INTEGER PRIMARY KEY, release_id TEXT, release_manifest_sha256 TEXT)"
        )
        lake_origin.write_lake_store_origin(setup, original)
        setup.commit()

    connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(lake_origin.sqlite3, "connect", tracked_connect)
    if operation == "read":
        assert lake_origin.read_lake_store_origin(path) == original
    elif operation == "advance":
        lake_origin.advance_lake_store_origin(path, expected=original, target=target)
    else:
        with pytest.raises(LakeStoreOriginError, match="changed"):
            lake_origin.advance_lake_store_origin(path, expected=None, target=target)

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    with closing(connect(path)) as check:
        assert lake_origin.read_lake_store_origin_from_connection(check) == (
            target if operation == "advance" else original
        )
