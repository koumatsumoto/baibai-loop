from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from tests.engine.test_edinet_research_facts import facts
from tests.helpers.l1_release import lake as lake
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.market.edinet_facts.models import filing_identity
from baibai_engine.market.edinet_facts.store import store_facts
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.hydrate import hydrate_market_store
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.reader import (
    LakeReadError,
    accepted_dataset,
    materialize_partitions,
    resolve_release,
    selected_partitions,
)
from baibai_engine.storage.lake.release import create_l1_release
from baibai_engine.storage.lake.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.observations.schema import open_connection


def test_facts_export_fixed_read_and_hydrate(tmp_path, monkeypatch):
    names = ("edinet.segment_facts", "edinet.debt_schedule")
    narrow_release_policy(monkeypatch, datasets=names)
    db = tmp_path / "source.sqlite"
    store_facts(db, identity=filing_identity("1873", "S100YR5P", "2026-07-22 10:00"), facts=facts())
    mirror = tmp_path / "mirror"
    stamp = datetime(2026, 9, 19, tzinfo=UTC)
    with sealed_sqlite_snapshot(
        sqlite_path=db, mirror_root=mirror, snapshot_id="facts"
    ) as snapshot:
        paths = [
            export_dataset_snapshot(
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
    fixed = resolve_release(source, release.release_id, manifest_sha256=digest)
    cache = LakeObjectCache(root=mirror, source=source)
    with lake_session() as session:
        for name, alias in zip(names, ("segments", "debt"), strict=True):
            paths = materialize_partitions(
                cache,
                dataset=accepted_dataset(fixed, name),
                partitions=selected_partitions(fixed, name),
            )
            session.connection.read_parquet([str(path) for path in paths]).create_view(alias)
        result = session.connection.execute(
            "SELECT debt_category, sum(principal) AS principal FROM debt "
            "WHERE source_doc_id=$doc AND disclosed_on=$day AND due_from_months=12 "
            "GROUP BY debt_category ORDER BY debt_category",
            {"doc": "S100YR5P", "day": "2026-07-22"},
        ).fetchall()
        assert result == [("bonds", 130000000.0), ("long_term_borrowings", 1282000000.0)]
        before = session.connection.execute(
            "SELECT count(*) FROM segments WHERE disclosed_on BETWEEN $start AND $cutoff",
            {"start": "2025-01-01", "cutoff": "2026-07-21"},
        ).fetchone()
        assert before == (0,)  # comparative values are available on submission, not period_end
    target = tmp_path / "target.sqlite"
    open_connection(target).close()
    with lake_session() as session:
        report = hydrate_market_store(
            session,
            release=fixed,
            cache=cache,
            store=target,
            dataset_names=names,
        )
    assert report.rows == {"edinet.segment_facts": 40, "edinet.debt_schedule": 11}
    with closing(sqlite3.connect(target)) as conn, conn:
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


def test_old_fixed_release_without_optional_facts_remains_readable(lake):
    mirror, pointer = lake
    source = LocalMirrorSource(mirror)
    fixed = resolve_release(source, pointer.release_id, manifest_sha256=pointer.manifest_sha256)
    assert fixed.release_id == pointer.release_id
    assert "edinet.segment_facts" not in fixed.dataset_names()
    with pytest.raises(LakeReadError, match="does not contain dataset"):
        accepted_dataset(fixed, "edinet.segment_facts")
    cache = LakeObjectCache(root=mirror, source=source)
    paths = materialize_partitions(
        cache,
        dataset=accepted_dataset(fixed, "jquants.daily_bars"),
        partitions=selected_partitions(fixed, "jquants.daily_bars"),
    )
    with lake_session() as session:
        assert session.connection.read_parquet([str(path) for path in paths]).count(
            "*"
        ).fetchone() == (4,)
