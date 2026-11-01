"""Jobs validation after synthetic provider acquisition and L2 readback."""

from datetime import timedelta

from tests.helpers.economic_wire import DAY, END, IDS, START, payload
from tests.helpers.economic_wire import wire as wire
from tests.helpers.network import _no_real_network as _no_real_network

from baibai_engine.data_collection.acquisition.refresh import (
    SourceRefreshFailure,
    SourceRefreshSuccess,
    refresh_sources,
)
from baibai_engine.data_collection.receipts.native_sources.store import open_read_only_connection
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)
from baibai_engine.machine_analysis.observations.economic_indicators.reader import (
    build_store_observation_reader,
)
from baibai_jobs.observability.source_stores import main as validate_stores


def test_service_writes_and_reads_real_temporary_stores_without_losing_prior_rows(
    wire, tmp_path, capsys
):
    definitions = load_definitions()
    by_id = definitions.by_id()
    database = tmp_path / "indicators.sqlite"
    source_ids = [by_id[series_id].source_id for series_id in IDS]

    def full_response(sid, url):
        current = payload(sid)
        if sid == "vix":
            current = (
                f"observation_date,VIXCLS\n{(DAY - timedelta(days=7)).isoformat()},19\n"
                f"{DAY.isoformat()},20\n"
            ).encode()
        else:
            previous = payload(
                sid, day=DAY - timedelta(days=7), value=400 if sid == "jp.foreign_flows" else 2900
            )
            current["data"] = previous["data"] + current["data"]
        return 200, {}, current

    wire.reply = full_response
    results = refresh_sources(source_ids, db_path=database, start=START, end=END)
    assert all(isinstance(r, SourceRefreshSuccess) for r in results)
    conn = open_read_only_connection(database)
    try:
        native_before = conn.execute(
            "SELECT * FROM economic_source_records ORDER BY source_id,observed_at,fetched_at"
        ).fetchall()
        reader = build_store_observation_reader(conn, series=definitions.series)
        for sid, value in zip(IDS, (500, 3000, 20), strict=True):
            rows = reader(sid, START, END)
            assert len(rows) == 2
            assert rows[-1].value == value
        before_publication = build_store_observation_reader(
            conn, series=definitions.series, vintage_cutoff=DAY + timedelta(days=5)
        )
        after_publication = build_store_observation_reader(
            conn, series=definitions.series, vintage_cutoff=DAY + timedelta(days=6)
        )
        assert before_publication(IDS[0], DAY, DAY) == ()
        assert len(after_publication(IDS[0], DAY, DAY)) == 1
    finally:
        conn.close()
    wire.reply = lambda sid, url: (403, {}, {})
    failures = refresh_sources(source_ids, db_path=database, start=START, end=END)
    assert all(isinstance(r, SourceRefreshFailure) for r in failures)
    conn = open_read_only_connection(database)
    try:
        assert (
            conn.execute(
                "SELECT * FROM economic_source_records ORDER BY source_id,observed_at,fetched_at"
            ).fetchall()
            == native_before
        )
        assert conn.execute("SELECT COUNT(*) FROM economic_source_runs").fetchone()[0] == 6
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    assert validate_stores(["--db", str(database)]) == 0
    assert "ok:" in capsys.readouterr().out
    assert len(wire.calls) == 6

