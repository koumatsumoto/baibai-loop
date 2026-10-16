"""Native source records survive arithmetic failures and replace outputs atomically."""

import io
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest

from baibai_engine.market_context import indicator_service
from baibai_engine.market_context.indicator_service import IndicatorsService
from baibai_engine.market_context.indicators.db import initialize_database, insert_observations
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.indicators.observations import ObservationRecord
from baibai_engine.market_context.indicators.providers.base import FetchContext, IndicatorsProviderError
from baibai_engine.market_context.indicators.providers.cftc import CftcSource
from baibai_engine.market_context.indicators.providers.ecb_fx import EcbFxSource
from baibai_engine.market_context.indicators.providers.estat_dashboard import (
    EStatDashboardSource,
)
from baibai_engine.market_context.indicators.read_contracts import native_source_key
from baibai_engine.market_context.indicators.source_snapshots import read_snapshots, store_snapshot
from baibai_engine.market_context.reading.reader import build_store_observation_reader
from baibai_engine.read_api.freshness import indicator_latest_observed_at
from tests.engine.market_context.indicators.indicator_fixtures import _FakeResponse, _StaticSession

DAY = date(2026, 10, 13)


def test_cftc_keeps_paired_native_values_and_empty_correction_hides_old_scalar(tmp_path, monkeypatch):
    database = tmp_path / "indicators.sqlite"
    series_id = "positioning.jpy_noncomm_net"
    row = {"noncomm_positions_long_all": "3", "noncomm_positions_short_all": "5", "unused": None}
    replies = [{DAY: [row]}, {}]
    monkeypatch.setattr(indicator_service, "fetch_native_records", lambda *a, **k: replies.pop(0))
    service = IndicatorsService(database)
    result = service.get_range(series_id, start=DAY, end=DAY, refresh=True)
    assert result.observations[0].value == -2.0
    with closing(sqlite3.connect(database)) as conn:
        assert list(read_snapshots(conn, DAY, DAY, source_key="cftc:097741"))[0].records == (row,)
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    assert service.get_range(series_id, start=DAY, end=DAY).cache_hit
    assert len(replies) == 1

    definition = load_definitions().by_id()[series_id]
    with closing(sqlite3.connect(database)) as conn, conn:
        insert_observations(conn, [ObservationRecord(
            series_id=series_id, observed_at=date(2026, 10, 14), value=999.0,
            unit="contracts", source_url=definition.source_url,
            vintage_at=datetime(2026, 10, 15, tzinfo=UTC),
        )])
    assert indicator_latest_observed_at(database) == DAY
    assert service.get_range(series_id, start=DAY, end=DAY, refresh=True).observations == ()
    with closing(sqlite3.connect(database)) as conn:
        read = build_store_observation_reader(conn, series=load_definitions().series)
        assert read(series_id, DAY, date(2026, 10, 14)) == ()
    assert indicator_latest_observed_at(database) is None


def test_ecb_crosses_use_one_native_row_and_missing_leg_never_uses_old_output(tmp_path):
    database = tmp_path / "indicators.sqlite"
    with closing(initialize_database(database)) as conn, conn:
        store_snapshot(conn, source_key="ecb_fx:reference", observed_at=DAY,
                       fetched_at=datetime(2026, 10, 14, tzinfo=UTC),
                       records=[{"Date": DAY.isoformat(), "JPY": "180", "USD": "1.2", "AUD": "N/A"}])
    with closing(sqlite3.connect(database)) as conn:
        read = build_store_observation_reader(conn, series=load_definitions().series)
        assert read("usd_jpy", DAY, DAY)[0].value == 150.0
        assert read("eur_jpy", DAY, DAY)[0].value == 180.0
        assert read("aud_jpy", DAY, DAY) == ()


def test_unusable_calculation_does_not_roll_back_native_bulk_records(tmp_path, monkeypatch):
    database = tmp_path / "indicators.sqlite"
    first = date(2000, 1, 1)
    row = {"noncomm_positions_long_all": "3", "noncomm_positions_short_all": None}
    monkeypatch.setattr(indicator_service, "fetch_native_records", lambda *a, **k: {first: [row]})
    with pytest.raises(IndicatorsProviderError, match="no observations"):
        IndicatorsService(database).refresh_all_history("positioning.jpy_noncomm_net", end=first)
    with closing(sqlite3.connect(database)) as conn:
        assert list(read_snapshots(conn, first, first, source_key="cftc:097741"))[0].records == (row,)


def test_bulk_sources_share_response_bytes_and_do_not_project_away_native_fields():
    definitions = load_definitions().by_id()
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("eurofxref-hist.csv", "Date,JPY,USD,AUD,CHF\n2026-10-13,180,1.2,1.5,N/A\n")
    response = _FakeResponse(content.getvalue())
    session = _StaticSession(response)
    with FetchContext() as context:
        for series_id in ("usd_jpy", "eur_jpy", "aud_jpy"):
            rows = EcbFxSource().fetch_records(definitions[series_id], start=DAY, end=DAY,
                                             session=session, context=context)
            assert rows[DAY][0]["CHF"] == "N/A"
        assert len(context.bytes_cache) == 1
    cftc = [{"report_date_as_yyyy_mm_dd": DAY.isoformat(), "cftc_contract_market_code": "097741",
             "noncomm_positions_long_all": "0", "noncomm_positions_short_all": "5", "extra": "kept"}]
    params_seen = []
    class Session(_StaticSession):
        def get(self, url, **kwargs):
            params_seen.append(kwargs["params"])
            return super().get(url, **kwargs)
    rows = CftcSource().fetch_records(definitions["positioning.jpy_noncomm_net"], start=DAY, end=DAY,
                                    session=Session(_FakeResponse(json.dumps(cftc).encode())))
    assert rows[DAY][0] == cftc[0]
    assert "$select" not in params_seen[0]


@pytest.mark.parametrize(("long", "short", "expected"), [("0", "0", 0.0), ("0", "5", -5.0), ("5", None, None)])
def test_native_positioning_preserves_zero_and_missingness(tmp_path, long, short, expected):
    database = tmp_path / "indicators.sqlite"
    with closing(initialize_database(database)) as conn, conn:
        store_snapshot(conn, source_key="cftc:097741", observed_at=DAY,
                       fetched_at=datetime(2026, 10, 14, tzinfo=UTC),
                       records=[{"noncomm_positions_long_all": long, "noncomm_positions_short_all": short}])
        read = build_store_observation_reader(conn, series=load_definitions().series)
        result = read("positioning.jpy_noncomm_net", DAY, DAY)
        assert (result[0].value if result else None) == expected


def test_latest_rechecks_complete_but_empty_native_window(tmp_path, monkeypatch):
    monkeypatch.setattr(indicator_service, "_today_jst", lambda: DAY)
    row = {"noncomm_positions_long_all": "7", "noncomm_positions_short_all": "2"}
    replies = [{}, {DAY: [row]}]
    monkeypatch.setattr(indicator_service, "fetch_native_records", lambda *a, **k: replies.pop(0))
    service = IndicatorsService(tmp_path / "indicators.sqlite")
    series_id = "positioning.jpy_noncomm_net"
    assert service.get_range(series_id, start=DAY - timedelta(days=60), end=DAY, refresh=True).observations == ()
    latest = service.get_latest(series_id)
    assert latest.observations[0].value == 5.0
    assert replies == []


def test_conflicting_native_values_remain_stored_when_reading_refuses_them(tmp_path, monkeypatch):
    database = tmp_path / "indicators.sqlite"
    rows = [
        {"noncomm_positions_long_all": "7", "noncomm_positions_short_all": "2"},
        {"noncomm_positions_long_all": "7", "noncomm_positions_short_all": "3"},
    ]
    monkeypatch.setattr(indicator_service, "fetch_native_records", lambda *a, **k: {DAY: rows})
    with pytest.raises(ValueError, match="conflicting native inputs"):
        IndicatorsService(database).get_range("positioning.jpy_noncomm_net", start=DAY, end=DAY, refresh=True)
    with closing(sqlite3.connect(database)) as conn:
        assert list(read_snapshots(conn, DAY, DAY, source_key="cftc:097741"))[0].records == tuple(rows)


def test_dashboard_retains_publication_states_and_null_correction_without_old_scalar(tmp_path, monkeypatch):
    database = tmp_path / "indicators.sqlite"
    series_id = "jp.unemployment"
    series = load_definitions().by_id()[series_id]
    may, june = date(2026, 5, 1), date(2026, 6, 1)
    final = {"@time": "20260500", "@isProvisional": "0", "@unit": "001", "$": "2.4", "extra": None}
    provisional = {"@time": "20260600", "@isProvisional": "1", "@unit": "001", "$": "2.9"}
    correction = {**final, "$": "-"}
    june_final = {**provisional, "@isProvisional": "0", "$": "2.6"}
    replies = [{may: [final], june: [provisional]}, {may: [correction], june: [provisional, june_final]}]
    monkeypatch.setattr(indicator_service, "fetch_native_records", lambda *a, **k: replies.pop(0))
    service = IndicatorsService(database)
    result = service.get_range(series_id, start=may, end=june, refresh=True)
    assert [(item.observed_at, item.value) for item in result.observations] == [(may, 2.4)]
    with closing(sqlite3.connect(database)) as conn:
        snapshots = {item.observed_at: item.records for item in read_snapshots(
            conn, may, june, source_key=native_source_key(series)
        )}
        assert snapshots[may] == (final,)
        assert snapshots[june] == (provisional,)
        assert conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
    assert service.get_range(series_id, start=may, end=june).cache_hit
    assert len(replies) == 1
    result = service.get_range(series_id, start=may, end=june, refresh=True)
    assert [(item.observed_at, item.value) for item in result.observations] == [(june, 2.6)]
    with closing(sqlite3.connect(database)) as conn:
        read = build_store_observation_reader(conn, series=load_definitions().series)
        assert [(item.observed_at, item.value) for item in read(series_id, may, june)] == [(june, 2.6)]
        assert next(read_snapshots(conn, may, may, source_key=native_source_key(series))).records == (correction,)


def test_dashboard_keeps_native_input_when_numeric_reading_fails(tmp_path, monkeypatch):
    database = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["jp.unemployment"]
    row = {"@isProvisional": "0", "@unit": "001", "$": 2.4}
    monkeypatch.setattr(indicator_service, "fetch_native_records", lambda *a, **k: {DAY: [row]})
    with pytest.raises(ValueError, match="value is not a string"):
        IndicatorsService(database).get_range(series.series_id, start=DAY, end=DAY, refresh=True)
    with closing(sqlite3.connect(database)) as conn:
        assert next(read_snapshots(conn, DAY, DAY, source_key=native_source_key(series))).records == (row,)


def test_dashboard_live_response_shape_keeps_preliminary_wage_print(tmp_path, monkeypatch):
    # Public nominal-wage VALUE examples observed 2026-10-16 from the registered
    # getData endpoint. Only the response envelope is a reduced test fixture.
    series = load_definitions().by_id()["jp.nominal_wage_index"]
    july, august = date(2026, 7, 1), date(2026, 8, 1)
    final = {
        "@indicator": "0302030202010090010", "@unit": "000", "@stat": "00450071",
        "@regionCode": "00000", "@time": "20260700", "@cycle": "1", "@regionRank": "2",
        "@isSeasonal": "2", "@isProvisional": "0", "$": "114.7",
    }
    provisional = {**final, "@time": "20260800", "@isProvisional": "1", "$": "112.4"}
    payload = {"GET_STATS": {"RESULT": {"status": "0"}, "STATISTICAL_DATA": {
        "RESULT_INF": {"TOTAL_NUMBER": "2"},
        "DATA_INF": {"DATA_OBJ": [{"VALUE": final}, {"VALUE": provisional}]},
    }}}
    session = _StaticSession(_FakeResponse(json.dumps(payload).encode()))
    native = EStatDashboardSource().fetch_records(series, start=july, end=august, session=session)
    assert native == {july: [final], august: [provisional]}
    monkeypatch.setattr(indicator_service, "fetch_native_records", lambda *a, **k: native)
    database = tmp_path / "indicators.sqlite"
    result = IndicatorsService(database).get_range(series.series_id, start=july, end=august, refresh=True)
    assert [(item.observed_at, item.value, item.unit) for item in result.observations] == [(july, 114.7, "index")]
    with closing(sqlite3.connect(database)) as conn:
        stored = {item.observed_at: item.records for item in read_snapshots(
            conn, july, august, source_key=native_source_key(series)
        )}
        assert stored[july] == (final,)
        assert stored[august] == (provisional,)


def test_estat_missing_cell_replaces_prior_numeric_reading_without_erasing_native_fields(tmp_path, monkeypatch):
    database = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["jp.cpi.services"]
    day = date(2026, 5, 1)
    original = {"@time": "2026000505", "@unit": "指数", "$": "100.8", "@note": "published"}
    missing = {**original, "$": "***", "@note": "withdrawn value"}
    replies = [{day: [original]}, {day: [missing]}]
    monkeypatch.setattr(indicator_service, "fetch_native_records", lambda *a, **k: replies.pop(0))
    service = IndicatorsService(database)
    assert service.get_range(series.series_id, start=day, end=day, refresh=True).observations[0].value == 100.8
    assert service.get_range(series.series_id, start=day, end=day, refresh=True).observations == ()
    with closing(sqlite3.connect(database)) as conn:
        assert next(read_snapshots(conn, day, day, source_key=native_source_key(series))).records == (missing,)
        assert conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
        read = build_store_observation_reader(conn, series=load_definitions().series)
        assert read(series.series_id, day, day) == ()


def test_h15_one_native_package_drives_all_maturities_and_spread_without_scalar_revival(tmp_path, monkeypatch):
    database = tmp_path / "indicators.sqlite"
    definitions = load_definitions().by_id()
    maturities = ("us.10y", "us.2y", "us.30y")
    assert len({native_source_key(definitions[item]) for item in maturities}) == 1
    source_key = native_source_key(definitions["us.10y"])
    raw = {
        "Time Period": DAY.isoformat(),
        "RIFLGFCY10_N.B": "4.39",
        "RIFLGFCY02_N.B": "3.88",
        "RIFLGFCY30_N.B": "4.80",
        "UNUSED_NATIVE_COLUMN": "ND",
    }
    original = {"row": raw, "preamble": ["Synthetic H.15 package metadata"]}
    correction = {"row": {**raw, "RIFLGFCY10_N.B": "ND"}, "preamble": original["preamble"]}
    replies = [{DAY: [original]}, {DAY: [correction]}]
    monkeypatch.setattr(indicator_service, "fetch_native_records", lambda *a, **k: replies.pop(0))
    with closing(initialize_database(database)) as connection, connection:
        insert_observations(connection, [ObservationRecord(
            series_id="us.10y", observed_at=DAY, value=20.0, unit="percent",
            source_url=definitions["us.10y"].source_url,
            vintage_at=datetime(2026, 10, 14, tzinfo=UTC),
        )])
    service = IndicatorsService(database)
    first = service.get_range("us.10y", start=DAY, end=DAY, refresh=True)
    assert first.observations[0].value == 4.39
    for series_id, expected in (("us.2y", 3.88), ("us.30y", 4.8)):
        reading = service.get_range(series_id, start=DAY, end=DAY)
        assert reading.cache_hit
        assert reading.observations[0].value == expected
    spread = service.get_range("us.10y_2y_spread", start=DAY, end=DAY)
    assert spread.observations[0].value == pytest.approx(51.0)
    assert len(replies) == 1
    assert service.get_range("us.10y", start=DAY, end=DAY, refresh=True).observations == ()
    assert service.get_range("us.2y", start=DAY, end=DAY).observations[0].value == 3.88
    assert service.get_range("us.10y_2y_spread", start=DAY, end=DAY).observations == ()
    with closing(sqlite3.connect(database)) as connection:
        snapshot = next(read_snapshots(connection, DAY, DAY, source_key=source_key))
        assert snapshot.records == (correction,)
        read = build_store_observation_reader(connection, series=load_definitions().series)
        assert read("us.10y", DAY, DAY) == ()
        assert connection.execute("SELECT value FROM observations WHERE series_id = 'us.10y'").fetchone()[0] == 20.0


def test_h15_missing_requested_column_fails_only_after_native_rows_are_saved(tmp_path, monkeypatch):
    database = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["us.10y"]
    envelope = {"row": {"Time Period": DAY.isoformat(), "OTHER": "4.39"}, "preamble": []}
    monkeypatch.setattr(indicator_service, "fetch_native_records", lambda *a, **k: {DAY: [envelope]})
    with pytest.raises(ValueError):
        IndicatorsService(database).get_range("us.10y", start=DAY, end=DAY, refresh=True)
    with closing(sqlite3.connect(database)) as connection:
        assert next(read_snapshots(
            connection, DAY, DAY, source_key=native_source_key(series)
        )).records == (envelope,)

def test_latest_forced_refresh_repairs_malformed_native_cache_without_hiding_read_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(indicator_service, "_today_jst", lambda: DAY)
    database = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["jp.unemployment"]
    month = DAY.replace(day=1)
    malformed = {"@time": "20261000", "@isProvisional": "0", "@unit": "001", "$": 2.4}
    corrected = {**malformed, "$": "2.4"}
    replies = [{month: [malformed]}, {month: [corrected]}]
    monkeypatch.setattr(indicator_service, "fetch_native_records", lambda *a, **k: replies.pop(0))
    service = IndicatorsService(database)

    with pytest.raises(ValueError, match="value is not a string"):
        service.get_range(series.series_id, start=month, end=month, refresh=True)
    with pytest.raises(ValueError, match="value is not a string"):
        service.get_latest(series.series_id)
    assert len(replies) == 1

    result = service.get_latest(series.series_id, refresh=True)
    assert [(item.observed_at, item.value) for item in result.observations] == [(month, 2.4)]
    assert not result.cache_hit
    assert replies == []
    with closing(sqlite3.connect(database)) as conn:
        assert next(read_snapshots(conn, month, month, source_key=native_source_key(series))).records == (corrected,)
        assert conn.execute(
            "SELECT count(*) FROM indicator_source_snapshots WHERE source_key = ? AND observed_at = ?",
            (native_source_key(series), month.isoformat()),
        ).fetchone()[0] == 2





def test_multpl_native_correction_hides_old_scalar(tmp_path, monkeypatch):
    database = tmp_path / "indicators.sqlite"
    series_id = "us.sp500_cape"
    definition = load_definitions().by_id()[series_id]
    day = date(2026, 10, 15)
    source_key = native_source_key(definition)
    assert source_key == "multpl:https://www.multpl.com/shiller-pe/table/by-month"
    first = {day: [{"date_text": "Oct 15, 2026", "value_text": "41.01", "table_row": 0}]}
    corrected = {day: [{"date_text": "Oct 15, 2026", "value_text": "null", "table_row": 0}]}
    replies = [first, corrected]
    monkeypatch.setattr(indicator_service, "fetch_native_records", lambda *a, **k: replies.pop(0))
    service = IndicatorsService(database)
    assert service.get_range(series_id, start=day, end=day, refresh=True).observations[0].value == 41.01
    with closing(sqlite3.connect(database)) as conn, conn:
        insert_observations(conn, [ObservationRecord(
            series_id=series_id, observed_at=day, value=20.0, unit=definition.unit,
            source_url="https://www.multpl.com/shiller-pe", vintage_at=datetime.now(UTC),
        )])
    assert service.get_range(series_id, start=day, end=day, refresh=True).observations == ()
    with closing(sqlite3.connect(database)) as conn:
        assert list(read_snapshots(conn, day, day, source_key=source_key))[0].records == tuple(corrected[day])
        reader = build_store_observation_reader(conn, series=load_definitions().series)
        assert reader(series_id, day, day) == ()


def test_multpl_same_date_conflict_retains_both_cells_and_fails_l2(tmp_path):
    database = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["us.sp500_cape"]
    day = date(2026, 10, 1)
    records = [{"date_text": "Oct 1, 2026", "value_text": "41.01", "table_row": 0},
               {"date_text": "Oct 1, 2026", "value_text": "40.81", "table_row": 1}]
    with closing(initialize_database(database)) as conn, conn:
        store_snapshot(conn, source_key=native_source_key(series), observed_at=day,
                       fetched_at=datetime.now(UTC), records=records)
    with closing(sqlite3.connect(database)) as conn:
        assert list(read_snapshots(conn, day, day, source_key=native_source_key(series)))[0].records == tuple(records)
        reader = build_store_observation_reader(conn, series=load_definitions().series)
        with pytest.raises(ValueError, match="conflicting native inputs"):
            reader(series.series_id, day, day)


def test_multpl_missing_duplicate_does_not_hide_missingness(tmp_path):
    database = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["us.sp500_cape"]
    day = date(2026, 10, 1)
    with closing(initialize_database(database)) as conn, conn:
        store_snapshot(conn, source_key=native_source_key(series), observed_at=day,
                       fetched_at=datetime.now(UTC), records=[
                           {"date_text": "Oct 1, 2026", "value_text": "null", "table_row": 0},
                           {"date_text": "Oct 1, 2026", "value_text": "40.81", "table_row": 1},
                       ])
    with closing(sqlite3.connect(database)) as conn:
        reader = build_store_observation_reader(conn, series=load_definitions().series)
        assert reader(series.series_id, day, day) == ()


def test_multpl_ratio_rejects_percent_cell_after_persistence(tmp_path):
    database = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["us.sp500_cape"]
    day = date(2026, 10, 1)
    with closing(initialize_database(database)) as conn, conn:
        store_snapshot(conn, source_key=native_source_key(series), observed_at=day,
                       fetched_at=datetime.now(UTC), records=[
                           {"date_text": "Oct 1, 2026", "value_text": "40.81%", "table_row": 0},
                       ])
    with closing(sqlite3.connect(database)) as conn:
        reader = build_store_observation_reader(conn, series=load_definitions().series)
        with pytest.raises(ValueError, match="invalid multpl cell"):
            reader(series.series_id, day, day)


def test_multpl_percent_with_dagger_is_interpreted_only_in_l2(tmp_path):
    database = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["us.sp500_earnings_yield"]
    day = date(2026, 10, 1)
    row = {"date_text": "Oct 1, 2026", "value_text": "† 3.18%", "table_row": 1}
    with closing(initialize_database(database)) as conn, conn:
        store_snapshot(conn, source_key=native_source_key(series), observed_at=day,
                       fetched_at=datetime.now(UTC), records=[row])
    with closing(sqlite3.connect(database)) as conn:
        assert list(read_snapshots(conn, day, day, source_key=native_source_key(series)))[0].records == (row,)
        reader = build_store_observation_reader(conn, series=load_definitions().series)
        assert reader(series.series_id, day, day)[0].value == 3.18


@pytest.mark.parametrize("cell", ["not-a-number", "1,,2"])
def test_multpl_malformed_cell_fails_only_at_reading_boundary(tmp_path, cell):
    database = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["us.sp500_cape"]
    day = date(2026, 10, 1)
    row = {"date_text": "Oct 1, 2026", "value_text": cell, "table_row": 1}
    with closing(initialize_database(database)) as conn, conn:
        store_snapshot(conn, source_key=native_source_key(series), observed_at=day,
                       fetched_at=datetime.now(UTC), records=[row])
    with closing(sqlite3.connect(database)) as conn:
        assert list(read_snapshots(conn, day, day, source_key=native_source_key(series)))[0].records == (row,)
        reader = build_store_observation_reader(conn, series=load_definitions().series)
        with pytest.raises(ValueError, match="invalid multpl cell"):
            reader(series.series_id, day, day)


def test_multpl_range_check_remains_at_shared_reading_boundary(tmp_path):
    database = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["us.sp500_cape"]
    day = date(2026, 10, 1)
    with closing(initialize_database(database)) as conn, conn:
        store_snapshot(conn, source_key=native_source_key(series), observed_at=day,
                       fetched_at=datetime.now(UTC), records=[
                           {"date_text": "Oct 1, 2026", "value_text": "999", "table_row": 1},
                       ])
    with closing(sqlite3.connect(database)) as conn:
        reader = build_store_observation_reader(conn, series=load_definitions().series)
        with pytest.raises(ValueError, match="outside plausible range"):
            reader(series.series_id, day, day)
