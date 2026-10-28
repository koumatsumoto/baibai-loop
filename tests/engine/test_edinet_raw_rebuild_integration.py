"""A fixed release of EDINET originals can rebuild lists, metrics and facts offline."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
import requests
from tests.engine.test_edinet_research_facts import source_zip
from tests.engine.test_source_maintenance_cli import _edinet_csv_zip, _edinet_document
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
from baibai_engine.data_collection.providers.edinet.receipts import retain_list_response
from baibai_jobs.maintenance.market import cli as app
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.hydrate import hydrate_market_store
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def test_raw_only_fixed_release_rebuilds_edinet_lists_metrics_and_facts(tmp_path, monkeypatch):
    names = ("edinet.list_responses", "edinet.source_archives")
    narrow_release_policy(monkeypatch, datasets=names, carries_history=None)
    day = date(2026, 7, 22)
    doc_id = "S100YR5P"
    document = _edinet_document(ticker="18730", doc_id=doc_id)
    document.update(
        seqNumber=1,
        docInfoEditStatus="0",
        submitDateTime="2026-07-22 10:00",
        periodStart="2025-05-01",
        periodEnd="2026-04-30",
    )
    original = tmp_path / "source.sqlite"
    with closing(open_connection(original)) as conn:
        retain_list_response(
            conn,
            doc_date=day,
            received_at=datetime(2026, 7, 22, 2, tzinfo=UTC),
            body=json.dumps(
                {"metadata": {"resultset": {"count": 1}}, "results": [document]},
                ensure_ascii=False,
            ).encode(),
        )
        retain_source_archive(conn, doc_id, 5, _edinet_csv_zip(), stored_on=day)
        retain_source_archive(conn, doc_id, 1, source_zip(doc_id), stored_on=day)
        for table in (
            "edinet_document_lists",
            "edinet_documents",
            "edinet_metrics",
            "edinet_segment_facts",
            "edinet_debt_schedule",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)

    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=original, mirror_root=mirror) as snapshot:
        exports = [
            export_dataset_snapshot(
                dataset_name=name,
                mirror_root=mirror,
                source_snapshot=snapshot,
                producer_git_commit="a" * 40,
                build_id=f"raw-{name.replace('.', '-')}",
            )
            for name in names
        ]
    assert {item.manifest.dataset: item.manifest.layer for item in exports} == dict.fromkeys(
        names, "l1_source"
    )
    manifest_path, release_manifest = create_release(
        dataset_manifest_paths=[item.manifest_path for item in exports],
        mirror_root=mirror,
        release_id="edinet-raw-only-rebuild",
    )
    assert set(release_manifest.datasets) == set(names)
    source = LocalMirrorSource(mirror)
    fixed = resolve_release(
        source,
        release_manifest.release_id,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    target = tmp_path / "market.sqlite"
    open_connection(target).close()

    def forbidden(*_args, **_kwargs):
        pytest.fail("offline EDINET rebuild attempted provider or HTTP setup")

    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    monkeypatch.setattr(app, "load_project_env", lambda: None)
    monkeypatch.setattr(
        app,
        "SourceMaintenanceConfig",
        type(
            "OfflineConfig",
            (),
            {"from_env": staticmethod(lambda: SimpleNamespace(sqlite_cache_dir=tmp_path))},
        ),
    )
    monkeypatch.setattr(app, "EDINETRefresh", forbidden)
    monkeypatch.setattr(requests, "Session", forbidden)
    with lake_session() as session:
        report = hydrate_market_store(
            session,
            release=fixed,
            cache=LakeObjectCache(root=mirror, source=source),
            store=target,
            dataset_names=names,
        )
    assert report.rows == {"edinet.list_responses": 1, "edinet.source_archives": 2}
    with closing(sqlite3.connect(target)) as conn:
        for table in (
            "edinet_document_lists",
            "edinet_documents",
            "edinet_metrics",
            "edinet_segment_facts",
            "edinet_debt_schedule",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert conn.execute(
            "SELECT doc_id, source_type FROM edinet_source_archives ORDER BY source_type"
        ).fetchall() == [(doc_id, 1), (doc_id, 5)]

    assert (
        app.main(
            [
                "rebuild-edinet-lists",
                "--start",
                day.isoformat(),
                "--end",
                day.isoformat(),
                "--sqlite-path",
                str(target),
            ]
        )
        == 0
    )
    assert (
        app.main(
            [
                "extract-edinet-metrics",
                "--asof",
                day.isoformat(),
                "--lookback-days",
                "0",
            ]
        )
        == 0
    )
    assert (
        app.main(
            [
                "extract-edinet-facts",
                "--asof",
                day.isoformat(),
                "--initialize",
                "--sqlite-path",
                str(target),
            ]
        )
        == 0
    )
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute(
            "SELECT doc_id, sec_code, submit_datetime FROM edinet_documents"
        ).fetchall() == [(doc_id, "18730", "2026-07-22 10:00")]
        assert conn.execute("SELECT COUNT(*) FROM edinet_document_lists").fetchone() == (1,)
        assert conn.execute(
            "SELECT ticker, source_doc_id, net_cash, fcf_ttm FROM edinet_metrics"
        ).fetchall() == [("1873", doc_id, 600.0, 700.0)]
        assert conn.execute("SELECT COUNT(*) FROM edinet_segment_facts").fetchone() == (40,)
        assert conn.execute("SELECT COUNT(*) FROM edinet_debt_schedule").fetchone() == (11,)
        assert conn.execute(
            "SELECT value FROM edinet_segment_facts WHERE source_doc_id=? AND "
            "segment_name='住宅事業' AND period_end='2026-04-30' AND metric='segment_profit'",
            (doc_id,),
        ).fetchall() == [(3331000000.0,)]
        assert conn.execute(
            "SELECT principal FROM edinet_debt_schedule WHERE source_doc_id=? AND "
            "debt_category='long_term_borrowings' AND due_from_months=12",
            (doc_id,),
        ).fetchall() == [(1282000000.0,)]
