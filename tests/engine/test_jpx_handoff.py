from contextlib import closing
from datetime import UTC, date, datetime, timedelta

from tests.engine.test_jpx_refresh import FILE_A, FIXTURES, SOURCE_FILES, _workbook

from baibai_engine.data_collection.providers.jpx.models import JPXResponse
from baibai_engine.data_collection.providers.jpx.receipts import (
    complete_bundle,
    create_bundle,
    retain_bundle_response,
)
from baibai_jobs.integration.jpx_handoff import export_handoff, import_handoff
from baibai_engine.machine_analysis.observations.disclosures.jpx_rebuild import rebuild_jpx_snapshot
from baibai_engine.machine_analysis.observations.disclosures.jpx_sources import JPX_EARNINGS_CALENDAR_INDEX_URL, JPX_REGULATION_URLS
from baibai_engine.storage.observations.schema import open_connection

DAY = date(2026, 10, 15)
STAMP = datetime(2026, 10, 15, 7, tzinfo=UTC)


def earnings(path, stamp, announcement, ticker, *, invalid=False):
    resources = (("index", JPX_EARNINGS_CALENDAR_INDEX_URL), ("workbook:0", FILE_A))
    with closing(open_connection(path)) as conn:
        bundle = create_bundle(conn, "earnings_calendar", DAY, stamp, resources)
        retain_bundle_response(
            conn,
            bundle,
            "index",
            JPXResponse(resources[0][1], stamp, "text/html", b"<a href='kessan.xlsx'>daily</a>"),
        )
        retain_bundle_response(
            conn,
            bundle,
            "workbook:0",
            JPXResponse(
                FILE_A,
                stamp,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                b"invalid" if invalid else _workbook("", [[announcement, ticker]]),
            ),
        )
        complete_bundle(conn, bundle)
    return bundle


def regulations(path, day, stamp):
    with closing(open_connection(path)) as conn:
        bundle = create_bundle(conn, "regulations", day, stamp, tuple(JPX_REGULATION_URLS.items()))
        for name, url in JPX_REGULATION_URLS.items():
            retain_bundle_response(
                conn,
                bundle,
                name,
                JPXResponse(url, stamp, "text/html", (FIXTURES / SOURCE_FILES[name]).read_bytes()),
            )
        complete_bundle(conn, bundle)
    return bundle


def test_late_global_earnings_bundle_preserves_latest_complete_workbook(tmp_path):
    source, target, stage = (
        tmp_path / name for name in ("source.sqlite", "target.sqlite", "stage.sqlite")
    )
    old = earnings(source, STAMP, "2026-10-20", "130A")
    newer = earnings(target, STAMP + timedelta(hours=1), "2026-10-22", "7203")
    rebuild_jpx_snapshot(target, newer)
    with closing(open_connection(target)) as conn, conn:
        conn.execute(
            "INSERT INTO jpx_regulation_sources VALUES (?, ?, ?)",
            (DAY.isoformat(), "other-source", STAMP.isoformat()),
        )
        expected = conn.execute("SELECT * FROM jpx_earnings_calendar").fetchall()
    selected = export_handoff(source, stage, since=STAMP, collection="earnings_calendar")
    assert selected == (old,)
    result = import_handoff(stage, target, selected, collection="earnings_calendar")
    assert result["replayed_bundles"] == [newer]
    with closing(open_connection(target)) as conn:
        assert conn.execute("SELECT * FROM jpx_earnings_calendar").fetchall() == expected
        assert conn.execute("SELECT COUNT(*) FROM jpx_source_bundles").fetchone()[0] == 2
        assert (
            conn.execute("SELECT source_name FROM jpx_regulation_sources").fetchone()[0]
            == "other-source"
        )
    with closing(open_connection(stage)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jpx_earnings_calendar").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM source_coverage").fetchone()[0] == 0
    assert (
        import_handoff(stage, target, selected, collection="earnings_calendar")["raw_rows_added"]
        == 0
    )


def test_regulations_replay_only_the_selected_asof_day(tmp_path):
    source, target, stage = (
        tmp_path / name for name in ("source.sqlite", "target.sqlite", "stage.sqlite")
    )
    regulations(source, DAY, STAMP)
    next_day = DAY + timedelta(days=1)
    latest = regulations(target, next_day, STAMP + timedelta(days=1))
    rebuild_jpx_snapshot(target, latest)
    with closing(open_connection(target)) as conn:
        before = conn.execute("SELECT * FROM jpx_regulation_flags ORDER BY 1, 2, 3").fetchall()
    selected = export_handoff(source, stage, since=STAMP, collection="regulations")
    import_handoff(stage, target, selected, collection="regulations")
    with closing(open_connection(target)) as conn:
        assert (
            conn.execute(
                "SELECT * FROM jpx_regulation_flags WHERE asof_date = ? ORDER BY 1, 2, 3",
                (next_day.isoformat(),),
            ).fetchall()
            == before
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM jpx_regulation_flags WHERE asof_date = ?", (DAY.isoformat(),)
            ).fetchone()[0]
            > 0
        )


def test_invalid_complete_bundle_is_retained_but_does_not_claim_projection_success(tmp_path):
    source, target, stage = (
        tmp_path / name for name in ("source.sqlite", "target.sqlite", "stage.sqlite")
    )
    prior = earnings(target, STAMP, "2026-10-20", "130A")
    rebuild_jpx_snapshot(target, prior)
    invalid = earnings(source, STAMP + timedelta(hours=1), "2026-10-22", "7203", invalid=True)
    selected = export_handoff(source, stage, since=STAMP, collection="earnings_calendar")
    result = import_handoff(stage, target, selected, collection="earnings_calendar")
    assert result["projection_failures"] == [invalid]
    assert result["replayed_bundles"] == []
    with closing(open_connection(target)) as conn:
        assert conn.execute("SELECT ticker FROM jpx_earnings_calendar").fetchone()[0] == "130A"
        assert conn.execute("SELECT COUNT(*) FROM jpx_source_bundles").fetchone()[0] == 2
