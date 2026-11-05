"""A successful FRED missing-cell revision cannot revive an earlier value."""

from __future__ import annotations

from contextlib import closing
from datetime import date
from unittest import TestCase

from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import _FakeResponse, _RaisingSession, _source, _StaticSession

import baibai_engine.data_collection.acquisition.refresh as acquisition
from baibai_engine.data_collection.providers import fred
from baibai_engine.data_collection.providers.fred import FredSource, parse_fred_csv
from baibai_engine.data_collection.receipts.native_sources import store as db
from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
from baibai_engine.data_collection.transport.context import IndicatorsProviderError
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)
from baibai_engine.machine_analysis.observations.economic_indicators.reader import (
    build_store_observation_reader,
)
from baibai_engine.machine_analysis.observations.economic_indicators.service import (
    IndicatorsService,
)


def test_missing_cell_is_retained_but_empty_full_history_does_not_erase_prior_values(
    tmp_path, monkeypatch
):
    path = tmp_path / "indicators.sqlite"
    series = load_definitions().by_id()["vix"]
    first, second = date(2026, 7, 24), date(2026, 7, 27)
    replies = iter(
        [
            "observation_date,VIXCLS,unused\n2026-07-24,20,kept\n2026-07-27,21,.\n",
            "observation_date,VIXCLS,unused\n2026-07-24,.,kept\n2026-07-27,21,.\n",
            "observation_date,VIXCLS,unused\n",
        ]
    )
    monkeypatch.setattr(fred, "fetch_text", lambda *args, **kwargs: next(replies))
    service = IndicatorsService(path)
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    original = service.get_range("vix", start=first, end=second)
    assert [(row.observed_at, row.value) for row in original.observations] == [
        (first, 20.0),
        (second, 21.0),
    ]
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    corrected = service.get_range("vix", start=first, end=second)
    assert [(row.observed_at, row.value) for row in corrected.observations] == [(second, 21.0)]
    with closing(db.open_read_only_connection(path)) as connection:
        reader = build_store_observation_reader(connection, series=load_definitions().series)
        assert reader("vix", first, first) == ()
        snapshot = next(read_snapshots(connection, first, first, source_id=series.source_id))
        assert snapshot.records == (
            {"observation_date": "2026-07-24", "VIXCLS": ".", "unused": "kept"},
        )
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=path, start=first, end=second
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshFailure)
    assert [
        (row.observed_at, row.value)
        for row in service.get_range("vix", start=first, end=second).observations
    ] == [(second, 21.0)]


class NativeParserTests(TestCase):
    def test_parse_fred_csv_keeps_all_received_periods_and_missing_cells(self) -> None:
        series = _source("fred_csv", "DGS10")
        text = "observation_date,DGS10\n2026-05-01,4.39\n2026-05-02,.\n2026-05-04,4.45\n"

        observations = parse_fred_csv(
            series,
            text,
        )

        self.assertEqual(
            observations,
            {
                date(2026, 5, 1): [{"observation_date": "2026-05-01", "DGS10": "4.39"}],
                date(2026, 5, 2): [{"observation_date": "2026-05-02", "DGS10": "."}],
                date(2026, 5, 4): [{"observation_date": "2026-05-04", "DGS10": "4.45"}],
            },
        )

    def test_parse_fred_csv_rejects_missing_date_header_instead_of_empty_history(self) -> None:
        series = _source("fred_csv", "DGS10")
        with self.assertRaisesRegex(IndicatorsProviderError, "missing column observation_date"):
            parse_fred_csv(
                series,
                "date,DGS10\n2026-05-01,4.39\n",
            )

    def test_fred_source_wraps_request_exception(self) -> None:
        series = _source("fred_csv", "DGS10")

        with self.assertRaisesRegex(IndicatorsProviderError, "failed to fetch"):
            FredSource().fetch_records(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=_RaisingSession(),
            )

    def test_fred_source_rejects_oversized_response(self) -> None:
        series = _source("fred_csv", "DGS10")

        with self.assertRaisesRegex(IndicatorsProviderError, "too large"):
            FredSource().fetch_records(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=_StaticSession(
                    _FakeResponse(
                        b"",
                        headers={"Content-Length": "9000000"},
                    )
                ),
            )


def test_recent_fetch_keeps_old_revision_and_replaces_only_real_periods(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot

    path = tmp_path / "source.sqlite"
    old, recent, artificial = date(2001, 1, 1), date(2026, 11, 1), date(2001, 1, 2)
    replies = iter(
        [
            "observation_date,VIXCLS\n2001-01-01,20\n2026-11-01,21\n",
            "observation_date,VIXCLS\n2001-01-01,22\n2026-11-01,21\n",
            "observation_date,VIXCLS\n2026-11-01,21\n",
        ]
    )
    monkeypatch.setattr(fred, "fetch_text", lambda *args, **kwargs: next(replies))
    with closing(db.initialize_database(path)) as conn, conn:
        store_snapshot(
            conn,
            source_id="fred-vixcls",
            observed_at=artificial,
            fetched_at=datetime(2001, 1, 3, tzinfo=UTC),
            records=[],
        )
    for value in ("20", "22", None):
        result = acquisition.refresh_sources(
            ["fred-vixcls"],
            db_path=path,
            start=None,
            end=recent,
            recent=True,
        )[0]
        assert isinstance(result, acquisition.SourceRefreshSuccess)
        with closing(db.open_read_only_connection(path)) as conn:
            snapshots = {
                item.observed_at: item.records
                for item in read_snapshots(conn, old, recent, source_id="fred-vixcls")
            }
            assert set(snapshots) == {old, recent, artificial}
            assert snapshots[old] == (
                () if value is None else ({"observation_date": str(old), "VIXCLS": value},)
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM economic_source_records WHERE observed_at=?",
                    (str(artificial),),
                ).fetchone()[0]
                == 1
            )
            assert {
                row[0]
                for row in conn.execute(
                    "SELECT observed_at FROM economic_source_record_confirmations"
                )
            } == {str(old), str(recent)}
            # The planned recent request is not relabelled a historical run.
            assert conn.execute("SELECT MIN(range_start) FROM economic_source_runs").fetchone()[
                0
            ] > str(old)


def test_short_csv_row_is_not_a_successful_period_deletion():
    import pytest

    with pytest.raises(IndicatorsProviderError, match="does not match"):
        parse_fred_csv(_source("fred_csv", "DGS10"), "observation_date,DGS10\n2026-05-01\n")


def test_recent_full_series_is_not_replayed_through_cached_gap_recovery(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from baibai_engine.data_collection.catalog import SourceDefinition
    from baibai_engine.data_collection.transport.context import FetchContext

    source = SourceDefinition(
        "fred-test", "fred_csv", "https://example.test/full.csv", "daily", "TEST"
    )
    body = b"observation_date,TEST\n2026-01-01,1\n2026-10-31,2\n"

    class Session(_StaticSession):
        calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return super().get(*args, **kwargs)

        def close(self):
            pass

    session = Session(_FakeResponse(body))

    class Context(FetchContext):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.session.close()
            self.session = session

    class Clock(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            return (datetime(2026, 11, 1, tzinfo=UTC) + timedelta(minutes=cls.calls)).astimezone(tz)

    path = tmp_path / "source.sqlite"
    with closing(db.initialize_database(path)) as connection, connection:
        db.record_source_run(
            connection,
            source_id=source.source_id,
            start=date(2026, 1, 1),
            end=date(2026, 1, 14),
            started_at=datetime(2026, 1, 14, tzinfo=UTC),
            status="ok",
            record_count=1,
        )
    monkeypatch.setattr(acquisition, "load_sources", lambda: {source.source_id: source})
    monkeypatch.setattr(acquisition, "FetchContext", Context)
    monkeypatch.setattr(acquisition, "datetime", Clock)
    result = acquisition.refresh_sources(
        [source.source_id], db_path=path, start=None, end=date(2026, 11, 1), recent=True
    )[0]
    assert isinstance(result, acquisition.SourceRefreshSuccess)
    assert result.record_count == 2
    assert session.calls == 1
    stamp = datetime(2026, 11, 1, 0, 2, tzinfo=UTC).isoformat(timespec="microseconds")
    with closing(db.open_read_only_connection(path)) as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT checked_at, adopted_fetched_at FROM economic_source_record_confirmations"
            )
        ] == [(stamp, stamp), (stamp, stamp)]
        assert connection.execute("SELECT COUNT(*) FROM economic_source_runs").fetchone()[0] == 2
