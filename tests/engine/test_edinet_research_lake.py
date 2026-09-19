from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

import pytest
from tests.engine.test_edinet_research_facts import facts
from tests.helpers.lake_policy import narrow_release_policy
from tests.tools.test_l1_mcp import lake  # noqa: F401 -- shared legacy-release fixture
from tools.l1_mcp.contract import L1Error, Query, ReleaseRef
from tools.l1_mcp.server import Adapter

from baibai_engine.market.edinet_facts.store import store_facts
from baibai_engine.market.lake.duck import lake_session
from baibai_engine.market.lake.hydrate import hydrate_market_store
from baibai_engine.market.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.market.lake.reader import resolve_release
from baibai_engine.market.lake.release import create_l1_release
from baibai_engine.market.lake.writer import export_legacy_sqlite, sealed_sqlite_snapshot
from baibai_engine.market.sqlite import open_connection


def test_facts_export_fixed_mcp_read_and_hydrate(tmp_path, monkeypatch):
    names = ("edinet.segment_facts", "edinet.debt_schedule")
    narrow_release_policy(monkeypatch, datasets=names)
    db = tmp_path / "source.sqlite"
    store_facts(db, doc_id="S100YR5P", disclosed_on="2026-07-22", facts=facts())
    mirror = tmp_path / "mirror"
    stamp = datetime(2026, 9, 19, tzinfo=UTC)
    with sealed_sqlite_snapshot(
        sqlite_path=db, mirror_root=mirror, snapshot_id="facts"
    ) as snapshot:
        paths = [
            export_legacy_sqlite(
                dataset_name=name,
                mirror_root=mirror,
                source_snapshot=snapshot,
                producer_git_commit="a" * 40,
                build_id=name.replace(".", "-"),
                created_at=stamp,
            ).manifest_path
            for name in names
        ]
    path, release = create_l1_release(
        dataset_manifest_paths=paths,
        mirror_root=mirror,
        release_id="facts-release",
        created_at=stamp,
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    source = LocalMirrorSource(mirror)
    adapter = Adapter(source=source)
    ref = ReleaseRef(release_id=release.release_id, manifest_sha256=digest)
    try:
        result = adapter.query(
            Query(
                release_ref=ref,
                sources=[
                    dict(
                        dataset=names[1], alias="debt", **{"from": "2026-07-22", "to": "2026-07-22"}
                    )
                ],
                sql="SELECT debt_category, sum(principal) AS principal FROM debt WHERE source_doc_id=$doc AND due_from_months=12 GROUP BY debt_category ORDER BY debt_category",
                parameters={"doc": "S100YR5P"},
            )
        )
        assert result["rows"] == [["bonds", 130000000.0], ["long_term_borrowings", 1282000000.0]]
        before = adapter.query(
            Query(
                release_ref=ref,
                sources=[
                    dict(
                        dataset=names[0],
                        alias="segments",
                        **{"from": "2025-01-01", "to": "2026-07-21"},
                    )
                ],
                sql="SELECT count(*) AS n FROM segments",
            )
        )
        assert before["rows"] == [
            [0]
        ]  # comparative values become available on submission, not period_end
    finally:
        adapter.close()
    fixed = resolve_release(source, release.release_id, manifest_sha256=digest)
    target = tmp_path / "target.sqlite"
    open_connection(target).close()
    with lake_session() as session:
        report = hydrate_market_store(
            session,
            release=fixed,
            cache=LakeObjectCache(root=mirror, source=source),
            store=target,
            dataset_names=names,
        )
    assert report.rows == {"edinet.segment_facts": 40, "edinet.debt_schedule": 11}
    with sqlite3.connect(target) as conn:
        assert (
            conn.execute(
                "select count(*) from edinet_segment_facts where value is null"
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "select sum(principal) from edinet_debt_schedule where due_from_months=12"
            ).fetchone()[0]
            == 1412000000
        )


def test_old_fixed_release_without_optional_facts_remains_readable(lake):  # noqa: F811
    mirror, pointer = lake
    adapter = Adapter(source=LocalMirrorSource(mirror))
    ref = ReleaseRef(release_id=pointer.release_id, manifest_sha256=pointer.manifest_sha256)
    try:
        assert adapter.fixed(ref).release_id == pointer.release_id
        with pytest.raises(L1Error, match="INVALID_ARGUMENT"):
            adapter.query(
                Query(
                    release_ref=ref,
                    sources=[
                        dict(
                            dataset="edinet.segment_facts",
                            alias="s",
                            **{"from": "2026-01-01", "to": "2026-09-19"},
                        )
                    ],
                    sql="SELECT * FROM s",
                )
            )
    finally:
        adapter.close()
