import hashlib
import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.external_assessments.staging import put_private
from baibai_jobs.maintenance import article_cutover


def test_stage_migration_keeps_original_bytes_and_operator_rows(tmp_path):
    source = tmp_path / "old.sqlite"
    with closing(open_connection(source)) as conn, conn:
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'article_%'"
        ).fetchall():
            conn.execute(f'DROP TABLE "{table}"')
        conn.execute("PRAGMA user_version=48")
        conn.execute(
            "INSERT INTO source_coverage VALUES ('synthetic', 'range', NULL, NULL, '2026-10-29', 0, 'ok', NULL)"
        )
    original = source.read_bytes()
    output = tmp_path / "new.sqlite"
    receipt = tmp_path / "conversion.json"
    article_cutover.prepare_copy(
        source, output, receipt, kind="stage", generation_directory=tmp_path
    )
    assert source.read_bytes() == original
    with closing(open_connection(output)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 49
        assert conn.execute("SELECT source FROM source_coverage").fetchall() == [("synthetic",)]
    with pytest.raises(FileExistsError):
        article_cutover.prepare_copy(
            source, output, receipt, kind="stage", generation_directory=tmp_path
        )


def test_partial_cutover_identifies_actual_uploaded_bytes_without_blind_resend(
    tmp_path, monkeypatch
):
    remote = MemoryR2Store()
    generations = {}
    for key in article_cutover.BUNDLE_KEYS:
        put_private(remote, key, f'{{"old":"{key}"}}'.encode(), previous_etag=None)
        generations[key] = remote.head(key).etag
        (tmp_path / f"{key}.etag").write_text(generations[key])
    candidate = b'{"new":"retained candidate"}'
    put_private(remote, "market.sqlite", candidate, previous_etag=generations["market.sqlite"])
    receipt = tmp_path / "cutover.json"
    market = tmp_path / "market.sqlite"
    open_connection(market).close()
    value = {
        "format": "article-cutover-48-49-v1",
        "kind": "canonical",
        "output": str(market.resolve()),
        "base_generations": generations,
        "base_origin": {"release_id": "old", "release_manifest_sha256": "a" * 64},
        "upload_candidates": {
            "market.sqlite": {
                "expected_etag": generations["market.sqlite"],
                "sha256": hashlib.sha256(candidate).hexdigest(),
            }
        },
    }
    receipt.write_text(json.dumps(value))
    from baibai_engine.storage.sqlite.source_cache.lake_origin import LakeStoreOrigin

    origin = LakeStoreOrigin("new", "b" * 64)
    monkeypatch.setattr(article_cutover, "_load_handle", lambda _: object())
    monkeypatch.setattr(article_cutover, "verify_held", lambda *_: None)
    monkeypatch.setattr(article_cutover, "read_lake_store_origin", lambda _: origin)
    monkeypatch.setattr(
        article_cutover,
        "_serving_pointer",
        lambda _: SimpleNamespace(
            pointer=SimpleNamespace(release_id="new", manifest_sha256="b" * 64)
        ),
    )
    generation = SimpleNamespace(
        metadata=SimpleNamespace(
            inputs={"market": SimpleNamespace(reference="new", sha256="b" * 64)}
        )
    )
    monkeypatch.setattr(
        article_cutover,
        "ModelGenerationStore",
        lambda _: SimpleNamespace(read=lambda _: generation),
    )
    kwargs = dict(
        receipt=receipt,
        market=market,
        runs=tmp_path / "runs.sqlite",
        generation_directory=tmp_path,
        lease_handle=tmp_path / "lease",
    )
    with pytest.raises(ValueError, match="remote bundle changed"):
        article_cutover.verify_cutover(remote, **kwargs)
    result = article_cutover.verify_cutover(remote, **kwargs, allow_partial=True)
    assert result["pending_keys"] == ["runs.sqlite", "indicators.sqlite"]
    assert "market.sqlite" in remote.get_keys
    before = remote.head("market.sqlite")
    put_private(remote, "market.sqlite", b'{"new":"other writer"}', previous_etag=before.etag)
    with pytest.raises(ValueError, match="differs from the recorded"):
        article_cutover.verify_cutover(remote, **kwargs, allow_partial=True)
