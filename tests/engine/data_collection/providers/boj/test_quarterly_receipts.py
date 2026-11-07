"""A quarterly response retains its actual periods, not a synthetic date range."""

import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
import requests
from tests.helpers.network import _no_real_network as _no_real_network

import baibai_engine.data_collection.acquisition.refresh as acquisition
from baibai_engine.data_collection.acquisition.policy import recovery_ranges
from baibai_engine.data_collection.catalog import load_sources
from baibai_engine.data_collection.receipts.native_sources import store
from baibai_engine.data_collection.receipts.native_sources.receipt_policy import retain_rechecks
from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)
from baibai_engine.machine_analysis.observations.economic_indicators.reader import (
    build_store_observation_reader,
)

END = date(2026, 11, 6)
STAMP = datetime(2026, 11, 6, 1, tzinfo=UTC)


def response(code, periods, values):
    return {
        "STATUS": 200,
        "MESSAGE": "synthetic",
        "RESULTSET": [
            {
                "SERIES_CODE": code,
                "UNIT": "D.I.",
                "VALUES": {"SURVEY_DATES": periods, "VALUES": values, "NOTE": "retained"},
            }
        ],
    }


def transport(monkeypatch, replies):
    replies = iter(replies)
    calls = []

    def get(self, url, **kwargs):
        calls.append((url, kwargs["params"]))
        result = requests.Response()
        result.status_code = 200
        result._content = json.dumps(next(replies)).encode()
        result._content_consumed = True
        return result

    monkeypatch.setattr(requests.Session, "get", get)
    return calls


@pytest.mark.parametrize("series_id", ["jp.tankan_large_mfg_di", "jp.tankan_large_nonmfg_di"])
def test_recent_retains_returned_revisions_nulls_and_future_periods_only(
    tmp_path, monkeypatch, series_id
):
    path = tmp_path / "receipts.sqlite"
    series = load_definitions().by_id()[series_id]
    source = load_sources()[series.source_id]
    code = source.provider_series_id.split(":")[1]
    calls = transport(
        monkeypatch,
        [
            response(code, [202401, 202402, 202603, 202604], [10, 20, 30, 40]),
            response(code, [202401, 202603, 202604], [11, None, 41]),
            response(code, [], []),
        ],
    )
    current = STAMP

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current.astimezone(tz or UTC)

    monkeypatch.setattr(acquisition, "datetime", Clock)
    first = acquisition.refresh_sources(
        [source.source_id], db_path=path, start=date(2024, 1, 1), end=END
    )[0]
    assert isinstance(first, acquisition.SourceRefreshSuccess)
    assert first.record_count == 4
    current += timedelta(hours=1)
    second = acquisition.refresh_sources(
        [source.source_id], db_path=path, start=None, end=END, recent=True
    )[0]
    assert isinstance(second, acquisition.SourceRefreshSuccess)
    assert second.record_count == 3
    assert second.start == END - timedelta(days=370)
    with closing(store.open_read_only_connection(path)) as conn:
        snapshots = list(
            read_snapshots(conn, date(2024, 1, 1), date(2026, 12, 31), source_id=source.source_id)
        )
        assert [(item.observed_at, item.records[0]["value"]) for item in snapshots] == [
            (date(2024, 3, 1), 11),
            (date(2024, 6, 1), 20),
            (date(2026, 9, 1), None),
            (date(2026, 12, 1), 41),
        ]
        assert [item.fetched_at for item in snapshots] == [current, STAMP, current, current]
        assert snapshots[0].records[0] == {
            "response_metadata": {"STATUS": 200, "MESSAGE": "synthetic"},
            "series_metadata": {"SERIES_CODE": code, "UNIT": "D.I."},
            "values_metadata": {"NOTE": "retained"},
            "survey_date": 202401,
            "value": 11,
        }
        reader = build_store_observation_reader(conn, series=load_definitions().series)
        assert reader(series_id, date(2026, 1, 1), END) == ()
        assert [
            (item.observed_at, item.value) for item in reader(series_id, date(2024, 1, 1), END)
        ] == [
            (date(2024, 3, 1), 11),
            (date(2024, 6, 1), 20),
        ]
        before = [tuple(row) for row in conn.execute("SELECT * FROM economic_source_records")]
        assert len(before) == 7
        assert (
            conn.execute("SELECT COUNT(*) FROM economic_source_record_confirmations").fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM economic_source_collection_confirmations"
            ).fetchone()[0]
            == 0
        )
    current += timedelta(hours=1)
    third = acquisition.refresh_sources(
        [source.source_id], db_path=path, start=None, end=END, recent=True
    )[0]
    assert isinstance(third, acquisition.SourceRefreshSuccess)
    assert third.record_count == 0
    with closing(store.open_read_only_connection(path)) as conn:
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM economic_source_records")
        ] == before
        assert [
            tuple(row)
            for row in conn.execute("SELECT range_start,range_end FROM economic_source_runs")
        ] == [
            ("2024-01-01", END.isoformat()),
            ((END - timedelta(days=370)).isoformat(), END.isoformat()),
            ((END - timedelta(days=370)).isoformat(), END.isoformat()),
        ]
    assert len(calls) == 3
    assert all(
        params == {"format": "json", "lang": "en", "db": "CO", "code": code} for _, params in calls
    )


def test_recent_gap_does_not_replay_the_same_response_with_a_later_receipt_clock(
    tmp_path, monkeypatch
):
    path = tmp_path / "receipts.sqlite"
    source = load_sources()["boj-tankan-lmfg"]
    code = source.provider_series_id.split(":")[1]
    calls = transport(monkeypatch, [response(code, [202401, 202603], [10, 30])])
    clocks = iter(STAMP + timedelta(seconds=offset) for offset in range(20))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(clocks).astimezone(tz or UTC)

    monkeypatch.setattr(acquisition, "datetime", Clock)
    with closing(store.initialize_database(path)) as conn, conn:
        store.record_source_run(
            conn,
            source_id=source.source_id,
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            started_at=STAMP - timedelta(days=1),
            status="ok",
            record_count=0,
        )
        assert recovery_ranges(conn, source, END)
    with retain_rechecks():
        result = acquisition.refresh_sources(
            [source.source_id], db_path=path, start=None, end=END, recent=True
        )[0]
    assert isinstance(result, acquisition.SourceRefreshSuccess)
    assert result.record_count == 2
    assert len(calls) == 1
    with closing(store.open_read_only_connection(path)) as conn:
        receipts = conn.execute(
            "SELECT observed_at,fetched_at FROM economic_source_records ORDER BY observed_at"
        ).fetchall()
        assert [tuple(row) for row in receipts] == [
            ("2024-03-01", (STAMP + timedelta(seconds=1)).isoformat(timespec="microseconds")),
            ("2026-09-01", (STAMP + timedelta(seconds=1)).isoformat(timespec="microseconds")),
        ]
        assert conn.execute("SELECT COUNT(*) FROM economic_source_runs").fetchone()[0] == 2
        assert recovery_ranges(conn, source, END)


@pytest.mark.parametrize(
    ("source_id", "periods", "first", "last", "expected"),
    [
        (
            "boj-timeseries-strdclucon",
            [20261001, 20261002, 20261003],
            date(2026, 10, 2),
            date(2026, 10, 2),
            date(2026, 10, 2),
        ),
        (
            "boj-cgpi",
            [202609, 202610, 202611],
            date(2026, 10, 1),
            date(2026, 10, 31),
            date(2026, 10, 1),
        ),
    ],
)
def test_daily_and_monthly_query_and_storage_ranges_are_unchanged(
    tmp_path, monkeypatch, source_id, periods, first, last, expected
):
    path = tmp_path / "receipts.sqlite"
    source = load_sources()[source_id]
    code = source.provider_series_id.split(":")[1]
    calls = transport(monkeypatch, [response(code, periods, [10, None, 30])])
    result = acquisition.refresh_sources([source_id], db_path=path, start=first, end=last)[0]
    assert isinstance(result, acquisition.SourceRefreshSuccess)
    assert result.record_count == 1
    assert calls[0][1]["startDate"] == first.strftime("%Y%m")
    assert calls[0][1]["endDate"] == last.strftime("%Y%m")
    with closing(store.open_read_only_connection(path)) as conn:
        snapshots = list(
            read_snapshots(conn, date(2026, 1, 1), date(2026, 12, 31), source_id=source_id)
        )
        assert len(snapshots) == (last - first).days + 1
        assert [item.observed_at for item in snapshots if item.records] == [expected]
        assert next(item for item in snapshots if item.records).records[0]["value"] is None
