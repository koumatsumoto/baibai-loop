from contextlib import closing
from datetime import timedelta

import pytest
import requests
from tests.engine.test_jquants_projection import DAY, FIRST_AT, Session, body, receive

from baibai_engine.collection.jquants.http import JQuantsAcquisitionError, JQuantsHTTP
from baibai_engine.collection.jquants.receipts import acquire_request
from baibai_engine.market.jquants_handoff import MARKET_SOURCES, export_handoff, import_handoff
from baibai_engine.market.jquants_projection import project_request
from baibai_engine.storage.observations.coverage import range_covered
from baibai_engine.storage.observations.schema import open_connection


def calendar(path, first, last, division, received):
    from datetime import date
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    rows = [{"Date": (start + timedelta(days=i)).isoformat(), "HolDiv": str(division)} for i in range((end-start).days+1)]
    return receive(path, "market_calendar", {"from": first, "to": last}, body(rows), received)


def test_late_old_calendar_keeps_its_nonoverlap_and_the_newer_overlap(tmp_path):
    source, target, stage = tmp_path / "source.sqlite", tmp_path / "target.sqlite", tmp_path / "stage.sqlite"
    old = calendar(source, "2026-10-01", "2026-10-10", 0, FIRST_AT)
    newer = calendar(target, "2026-10-05", "2026-10-15", 1, FIRST_AT + timedelta(hours=1))
    tail = calendar(target, "2026-10-12", "2026-10-20", 2, FIRST_AT + timedelta(hours=2))
    project_request(target, newer)
    project_request(target, tail)
    with closing(open_connection(target)) as conn, conn:
        conn.execute("INSERT INTO jpx_regulation_sources VALUES (?, ?, ?)", (DAY, "fresh-jpx", FIRST_AT.isoformat()))
    selected = export_handoff(source, stage, since=FIRST_AT, sources=MARKET_SOURCES)
    assert selected == (old,)
    result = import_handoff(stage, target, selected, sources=MARKET_SOURCES)
    assert result["replayed_requests"] == [old, newer, tail]
    with closing(open_connection(target)) as conn:
        assert conn.execute("SELECT day, holiday_division FROM jquants_market_calendar ORDER BY day").fetchall() == [
            (f"2026-10-{i:02d}", "0" if i < 5 else "1" if i < 12 else "2") for i in range(1, 21)
        ]
        assert conn.execute("SELECT source_name FROM jpx_regulation_sources").fetchone()[0] == "fresh-jpx"
        from datetime import date
        assert range_covered(conn, "jquants_market_calendar", date(2026, 10, 1), date(2026, 10, 20))
    assert import_handoff(stage, target, selected, sources=MARKET_SOURCES)["raw_pages_added"] == 0


def test_an_old_complete_daily_cross_section_cannot_replace_newer_canonical_rows(tmp_path):
    source, target, stage = tmp_path / "source.sqlite", tmp_path / "target.sqlite", tmp_path / "stage.sqlite"
    old = {"Code": "13010", "Date": DAY, "O": 100, "H": 100, "L": 100, "C": 100, "Vo": 1000, "AdjFactor": 1}
    current = {**old, "C": 200}
    receive(source, "daily_bars", {"date": DAY}, body([old]), FIRST_AT)
    newer = receive(target, "daily_bars", {"date": DAY}, body([current]), FIRST_AT + timedelta(hours=1))
    project_request(target, newer)
    with closing(open_connection(target)) as conn:
        before = conn.execute("SELECT * FROM jquants_daily_bars").fetchall()
    selected = export_handoff(source, stage, since=FIRST_AT, sources=MARKET_SOURCES)
    import_handoff(stage, target, selected, sources=MARKET_SOURCES)
    with closing(open_connection(target)) as conn:
        assert conn.execute("SELECT * FROM jquants_daily_bars").fetchall() == before


def test_incomplete_request_is_received_without_replacing_a_saved_cross_section(tmp_path):
    source, target, stage = tmp_path / "source.sqlite", tmp_path / "target.sqlite", tmp_path / "stage.sqlite"
    record = {"Code": "13010", "Date": DAY, "O": 100, "H": 100, "L": 100, "C": 100, "Vo": 1000, "AdjFactor": 1}
    current = receive(target, "daily_bars", {"date": DAY}, body([record]), FIRST_AT)
    project_request(target, current)
    # A pagination chain that has not reached its final page is still an original.
    client = JQuantsHTTP(
        "synthetic", session=Session(body([], cursor="next"), requests.Timeout()),
        clock=lambda: FIRST_AT + timedelta(hours=1),
    )
    with closing(open_connection(source)) as conn, pytest.raises(JQuantsAcquisitionError):
        acquire_request(conn, client, "daily_bars", {"date": DAY})
    selected = export_handoff(source, stage, since=FIRST_AT, sources=MARKET_SOURCES)
    with closing(open_connection(target)) as conn:
        before = conn.execute("SELECT * FROM jquants_daily_bars").fetchall()
    result = import_handoff(stage, target, selected, sources=MARKET_SOURCES)
    assert result["incomplete_requests"] == list(selected)
    with closing(open_connection(target)) as conn:
        assert conn.execute("SELECT * FROM jquants_daily_bars").fetchall() == before


def test_latest_rolling_calendar_does_not_reproject_older_history(tmp_path, monkeypatch):
    source, target, stage = tmp_path / "source.sqlite", tmp_path / "target.sqlite", tmp_path / "stage.sqlite"
    for offset in range(3):
        receipt = calendar(
            target, f"2026-10-{1+offset:02d}", f"2026-10-{10+offset:02d}", 0,
            FIRST_AT + timedelta(hours=offset),
        )
        project_request(target, receipt)
    latest = calendar(source, "2026-10-04", "2026-10-15", 1, FIRST_AT + timedelta(hours=4))
    selected = export_handoff(source, stage, since=FIRST_AT, sources=MARKET_SOURCES)
    calls = []
    def project(path, request_id):
        calls.append(request_id)
        return project_request(path, request_id)
    monkeypatch.setattr("baibai_engine.market.jquants_handoff.project_request", project)
    result = import_handoff(stage, target, selected, sources=MARKET_SOURCES)
    assert result["replayed_requests"] == calls == [latest]
    with closing(open_connection(target)) as conn:
        assert conn.execute("SELECT COUNT(DISTINCT request_id) FROM jquants_source_pages").fetchone()[0] == 4
        assert conn.execute("SELECT day, holiday_division FROM jquants_market_calendar ORDER BY day").fetchall() == [
            (f"2026-10-{i:02d}", "0" if i < 4 else "1") for i in range(1, 16)
        ]
