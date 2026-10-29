from contextlib import closing
from datetime import date

from baibai_engine.data_collection.providers.edinet.archives import retain_source_archive
from baibai_engine.storage.sqlite.source_cache.coverage import record_source_coverage
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.inputs import export_source_input


def test_financial_input_keeps_normal_empty_coverage_and_excludes_other_sources(tmp_path):
    canonical, scoped = tmp_path / "canonical.sqlite", tmp_path / "scoped.sqlite"
    with closing(open_connection(canonical)) as conn, conn:
        record_source_coverage(
            conn,
            source="jquants_fin_summaries",
            coverage_key="day",
            coverage_start="2026-10-15",
            coverage_end="2026-10-15",
            record_count=0,
            status="ok",
            error=None,
        )
        conn.execute("INSERT INTO jquants_market_calendar VALUES (?, ?)", ("2026-10-15", "1"))
        conn.execute(
            "INSERT INTO jpx_regulation_sources VALUES (?, ?, ?)",
            ("2026-10-15", "other", "2026-10-15T00:00:00+00:00"),
        )
    report = export_source_input(
        canonical, scoped, group="jquants-financial", asof=date(2026, 10, 15)
    )
    assert report["rows"] == {"jquants_fin_summaries": 0, "jquants_market_calendar": 1}
    with closing(open_connection(scoped)) as conn:
        assert conn.execute(
            "SELECT source, record_count, status FROM source_coverage"
        ).fetchall() == [("jquants_fin_summaries", 0, "ok")]
        assert conn.execute("SELECT COUNT(*) FROM jquants_market_calendar").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM jpx_regulation_sources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM lake_store_origin").fetchone()[0] == 0


def test_edinet_facts_acquisition_input_has_inventory_without_zip_payloads(tmp_path):
    canonical, scoped = tmp_path / "canonical.sqlite", tmp_path / "scoped.sqlite"
    with closing(open_connection(canonical)) as conn:
        retain_source_archive(conn, "S100XBRL", 1, b"large xbrl", stored_on=date(2026, 10, 15))
        retain_source_archive(conn, "S100CSV", 5, b"large csv", stored_on=date(2026, 10, 15))
    report = export_source_input(canonical, scoped, group="edinet-facts", asof=date(2026, 10, 15))
    assert report["archive_keys"] == [("S100XBRL", 1)]
    with closing(open_connection(scoped)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM edinet_source_archives").fetchone()[0] == 0
