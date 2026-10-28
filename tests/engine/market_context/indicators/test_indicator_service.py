from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from tests.helpers.indicator_store import (
    observation,
    store_h15_observations,
    store_mof_observations,
)

import baibai_engine.data_collection.acquisition.refresh as acquisition
from baibai_engine.cli.economic_indicators import (
    main,
)
from baibai_engine.data_collection.receipts.native_sources.records import (
    read_snapshots,
    store_snapshot,
)
from baibai_engine.data_collection.receipts.native_sources.store import (
    initialize_database,
    open_connection,
)
from baibai_engine.data_collection.transport.context import IndicatorsProviderError
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)
from baibai_engine.machine_analysis.observations.economic_indicators.service import (
    IndicatorsService,
)
from baibai_jobs.acquisition.economic_cli import main as economic_main


class IndicatorsServiceTests(unittest.TestCase):
    def test_derived_values_use_the_registry_bounds_at_the_calculation_boundary(self) -> None:
        for series_id, inputs in (
            ("jp.policy_path_gap", {"jp.2y": 12.0, "jp.policy_rate": 0.0}),
            ("us.erp", {"us.sp500_earnings_yield": 99.0, "us.10y": 4.0}),
        ):
            with self.subTest(series_id=series_id), tempfile.TemporaryDirectory() as tmp:
                database = Path(tmp) / "indicators.sqlite"
                with closing(initialize_database(database)) as conn:
                    if "jp.2y" in inputs:
                        store_mof_observations(
                            conn, [observation("jp.2y", date(2026, 7, 1), inputs["jp.2y"])]
                        )
                    if "jp.policy_rate" in inputs:
                        policy = load_definitions().by_id()["jp.policy_rate"]
                        store_snapshot(
                            conn,
                            source_id=policy.source_id,
                            observed_at=date(2026, 7, 1),
                            fetched_at=datetime.now(UTC),
                            records=[
                                {
                                    "series_metadata": {"SERIES_CODE": "STRDCLUCON"},
                                    "value": inputs["jp.policy_rate"],
                                }
                            ],
                        )
                    if "us.10y" in inputs:
                        store_h15_observations(
                            conn, [observation("us.10y", date(2026, 7, 1), inputs["us.10y"])]
                        )
                    conn.commit()

                if "us.sp500_earnings_yield" in inputs:
                    _store_multpl_earnings_yield(
                        database, date(2026, 7, 1), str(inputs["us.sp500_earnings_yield"])
                    )
                before = database.read_bytes()

                with self.assertRaisesRegex(ValueError, "outside plausible range"):
                    IndicatorsService(database).get_range(
                        series_id,
                        start=date(2026, 7, 1),
                        end=date(2026, 7, 31),
                    )
                self.assertEqual(database.read_bytes(), before)

    def test_collection_all_history_uses_the_jquants_rolling_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "indicators.sqlite"
            rows = {
                date(2026, 1, 1): [
                    {
                        "Section": "TSEPrime",
                        "PubDate": "2026-01-01",
                        "StDate": "2025-12-22",
                        "EnDate": "2025-12-26",
                        "FrgnBal": 1,
                    }
                ]
            }

            class FixedDatetime(datetime):
                @classmethod
                def now(cls, tz=None):
                    return cls(2026, 7, 20, tzinfo=tz)

            source_id = load_definitions().by_id()["jp.foreign_flows"].source_id
            with (
                patch.object(acquisition, "datetime", FixedDatetime),
                patch.object(acquisition, "fetch_native_records", return_value=rows) as fetch,
            ):
                outcome = acquisition.refresh_sources(
                    [source_id], db_path=database, start=None, end=date(2026, 1, 1)
                )[0]
            self.assertIsInstance(outcome, acquisition.SourceRefreshSuccess)
            result = IndicatorsService(database).get_range(
                "jp.foreign_flows", start=date(2025, 12, 26), end=date(2026, 1, 1)
            )
            self.assertEqual(fetch.call_args.kwargs["start"], date(2016, 7, 20))
            self.assertEqual(
                [(item.observed_at, item.value) for item in result.observations],
                [(date(2025, 12, 26), 1)],
            )
            with closing(sqlite3.connect(database)) as connection:
                received = next(
                    read_snapshots(
                        connection, date(2026, 1, 1), date(2026, 1, 1), source_id=source_id
                    )
                )
                self.assertEqual(received.records[0]["PubDate"], "2026-01-01")

    def test_collection_all_history_uses_boj_timeseries_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "indicators.sqlite"
            records = {
                date(1998, 1, 5): [
                    {
                        "series_metadata": {"SERIES_CODE": "STRDCLUCON"},
                        "survey_date": 19980105,
                        "value": 0.49,
                    }
                ]
            }
            source_id = load_definitions().by_id()["jp.policy_rate"].source_id
            with patch.object(acquisition, "fetch_native_records", return_value=records) as fetch:
                outcome = acquisition.refresh_sources(
                    [source_id], db_path=database, start=None, end=date(2026, 7, 20)
                )[0]
            self.assertIsInstance(outcome, acquisition.SourceRefreshSuccess)
            result = IndicatorsService(database).get_range(
                "jp.policy_rate", start=date(1998, 1, 1), end=date(2026, 7, 20)
            )
            self.assertEqual(fetch.call_args.kwargs["start"], date(1998, 1, 1))
            self.assertEqual(
                [(row.observed_at, row.value) for row in result.observations],
                [(date(1998, 1, 5), 0.49)],
            )

    def test_refresh_cli_requires_one_range_mode(self) -> None:
        with self.assertRaises(SystemExit):
            economic_main(
                [
                    "refresh",
                    "fed-h15",
                    "--start",
                    "2026-01-01",
                    "--all-history",
                    "--end",
                    "2026-07-20",
                ]
            )
        with self.assertRaises(SystemExit):
            economic_main(["refresh", "fed-h15", "--end", "2026-07-20"])

    def test_cli_handles_bad_db_path_without_traceback(self) -> None:
        self.assertEqual(main(["get", "us.10y", "--latest", "--db", "/tmp"]), 1)

    def test_cli_unknown_series_returns_controlled_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "indicators.sqlite"

            self.assertEqual(main(["get", "jp.cpi.stale", "--latest", "--db", str(db)]), 1)


def test_derived_read_uses_native_inputs_without_fetch_or_store_mutation(tmp_path):
    database = tmp_path / "indicators.sqlite"
    _store_multpl_earnings_yield(database, date(2026, 1, 1), "5.0")
    with closing(open_connection(database)) as connection:
        store_h15_observations(
            connection,
            [observation("us.10y", date(2026, 1, 30), 4.0, datetime(2026, 2, 1, tzinfo=UTC))],
        )
        connection.commit()
    before = database.read_bytes()
    with (
        patch(
            "baibai_engine.data_collection.acquisition.refresh.fetch_native_records",
            side_effect=AssertionError("derived read must not acquire native data"),
        ),
    ):
        service = IndicatorsService(database)
        ordinary = service.get_range("us.erp", start=date(2026, 1, 15), end=date(2026, 1, 31))
        repeated = service.get_range("us.erp", start=date(2026, 1, 15), end=date(2026, 1, 31))
        with patch(
            "baibai_engine.machine_analysis.observations.economic_indicators.service._today_jst",
            return_value=date(2026, 1, 31),
        ):
            latest = service.get_latest("us.erp")
    assert [(row.observed_at, row.value) for row in ordinary.observations] == [
        (date(2026, 1, 30), 1.0)
    ]
    assert repeated.observations == ordinary.observations
    assert latest.observations == ordinary.observations[-1:]
    assert database.read_bytes() == before
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM economic_source_runs").fetchone()[0] == 0


def test_empty_source_store_does_not_fill_missing_derived_inputs(tmp_path):
    database = tmp_path / "indicators.sqlite"
    initialize_database(database).close()
    result = IndicatorsService(database).get_range(
        "us.erp", start=date(2020, 1, 1), end=date(2026, 6, 30)
    )
    assert result.observations == ()


def test_derived_cli_reports_computation_without_source_fetch(tmp_path, capsys):
    database = tmp_path / "indicators.sqlite"
    _store_multpl_earnings_yield(database, date(2026, 1, 1), "5.0")
    with closing(open_connection(database)) as connection:
        store_h15_observations(
            connection,
            [observation("us.10y", date(2026, 1, 30), 4.0, datetime(2026, 2, 1, tzinfo=UTC))],
        )
        connection.commit()
    with (
        patch(
            "baibai_engine.data_collection.acquisition.refresh.fetch_native_records",
            side_effect=AssertionError("derived CLI must not acquire native data"),
        ),
    ):
        code = main(
            [
                "get",
                "us.erp",
                "--start",
                "2026-01-01",
                "--end",
                "2026-01-31",
                "--db",
                str(database),
            ]
        )
    assert code == 0
    assert capsys.readouterr().out.splitlines() == [
        "series_id\tobserved_at\tvalue\tunit\tprovider",
        "us.erp\t2026-01-30\t1\tpercent\tderived",
    ]


def _store_multpl_earnings_yield(database: Path, observed_at: date, value_text: str) -> None:
    """Seed the native table row read by the derived ERP tests, not an obsolete scalar."""
    series = load_definitions().by_id()["us.sp500_earnings_yield"]
    with closing(open_connection(database)) as conn:
        store_snapshot(
            conn,
            source_id=series.source_id,
            observed_at=observed_at,
            fetched_at=datetime(observed_at.year, observed_at.month, observed_at.day, tzinfo=UTC),
            records=[
                {
                    "date_text": f"{observed_at:%b} {observed_at.day}, {observed_at.year}",
                    "value_text": value_text,
                    "table_row": 1,
                }
            ],
        )
        conn.commit()


def _mof_rows(day, value):
    definition = load_definitions().by_id()["jp.10y"]
    return {day: [{"row": {"10年": str(value)}, "source_url": definition.source_url}]}


def test_native_refresh_retries_transport_without_discarding_the_success(tmp_path, monkeypatch):
    database = tmp_path / "indicators.sqlite"
    day = date(2026, 7, 20)
    contexts = []

    def fetch(source, **kwargs):
        context = kwargs["context"]
        contexts.append(context)
        if len(contexts) == 1:
            context.bytes_cache[("fixture", ())] = b"unusable"
            raise IndicatorsProviderError("temporary upstream error")
        assert context.bytes_cache == {}
        return _mof_rows(day, 1.5)

    monkeypatch.setattr(acquisition, "fetch_native_records", fetch)
    sleep = Mock()
    monkeypatch.setattr(acquisition.time, "sleep", sleep)
    source_id = load_definitions().by_id()["jp.10y"].source_id
    outcome = acquisition.refresh_sources([source_id], db_path=database, start=day, end=day)[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    result = IndicatorsService(database).get_range("jp.10y", start=day, end=day)
    assert [item.value for item in result.observations] == [1.5]
    assert len(contexts) == 2
    sleep.assert_called_once()
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT status, record_count FROM economic_source_runs").fetchall() == [
            ("ok", 1)
        ]


def test_refresh_continues_other_sources_and_shares_transport_context(tmp_path, monkeypatch):
    database = tmp_path / "indicators.sqlite"
    day = date(2026, 7, 20)
    contexts = []

    def fetch(source, **kwargs):
        contexts.append(kwargs["context"])
        if source.provider == "mof_jgb":
            raise RuntimeError("source parser changed")
        return {day: [{"row": {"Time Period": day.isoformat(), "RIFLGFCY10_N.B": "4.2"}}]}

    monkeypatch.setattr(acquisition, "fetch_native_records", fetch)
    definitions = load_definitions().by_id()
    outcomes = acquisition.refresh_sources(
        [definitions[series_id].source_id for series_id in ("jp.10y", "us.10y")],
        db_path=database,
        start=day,
        end=day,
    )
    assert isinstance(outcomes[0], acquisition.SourceRefreshFailure)
    assert outcomes[0].message == "source parser changed"
    assert isinstance(outcomes[1], acquisition.SourceRefreshSuccess)
    assert (
        IndicatorsService(database).get_range("us.10y", start=day, end=day).observations[0].value
        == 4.2
    )
    assert len(contexts) == 2
    assert contexts[0] is contexts[1]


def test_native_revisions_are_idempotent_and_survive_a_later_source_failure(tmp_path, monkeypatch):
    from baibai_engine.data_collection.transport.context import ProviderFetchStoppedError

    database = tmp_path / "indicators.sqlite"
    day = date(2026, 7, 20)
    received = iter([_mof_rows(day, 1.5), _mof_rows(day, 1.5), _mof_rows(day, 1.6)])
    monkeypatch.setattr(acquisition, "fetch_native_records", lambda *a, **k: next(received))
    service = IndicatorsService(database)
    source_id = load_definitions().by_id()["jp.10y"].source_id
    for expected in (1.5, 1.5, 1.6):
        outcome = acquisition.refresh_sources([source_id], db_path=database, start=day, end=day)[0]
        assert isinstance(outcome, acquisition.SourceRefreshSuccess)
        assert service.get_range("jp.10y", start=day, end=day).observations[0].value == expected
    revised = service.get_range("jp.10y", start=day, end=day)
    assert revised.observations[0].value == 1.6

    def fail(*args, **kwargs):
        raise ProviderFetchStoppedError("source unavailable")

    monkeypatch.setattr(acquisition, "fetch_native_records", fail)
    failure = acquisition.refresh_sources([source_id], db_path=database, start=day, end=day)[0]
    assert isinstance(failure, acquisition.SourceRefreshFailure)
    assert "source unavailable" in failure.message
    assert service.get_range("jp.10y", start=day, end=day).observations == revised.observations
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 2


def test_latest_read_does_not_reacquire_stale_inputs(tmp_path, monkeypatch):
    today = date(2026, 7, 27)
    stale = today - timedelta(days=8)
    replies = iter([_mof_rows(stale, 1.5), _mof_rows(today, 1.6)])
    calls = []

    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return next(replies)

    import baibai_engine.machine_analysis.observations.economic_indicators.service as service_module

    monkeypatch.setattr(acquisition, "fetch_native_records", fetch)
    monkeypatch.setattr(service_module, "_today_jst", lambda: today)
    database = tmp_path / "indicators.sqlite"
    service = IndicatorsService(database)
    source_id = load_definitions().by_id()["jp.10y"].source_id
    outcome = acquisition.refresh_sources(
        [source_id], db_path=database, start=today - timedelta(days=14), end=today
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    first = service.get_latest("jp.10y")
    cached = service.get_latest("jp.10y")
    assert first.observations == cached.observations
    assert cached.observations[0].observed_at == stale
    assert len(calls) == 1
    outcome = acquisition.refresh_sources(
        [source_id], db_path=database, start=today - timedelta(days=14), end=today
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    fresh = service.get_latest("jp.10y")
    assert fresh.observations[0].observed_at == today
    assert len(calls) == 2


def test_unknown_refresh_does_not_open_or_modify_the_store(tmp_path):
    database = tmp_path / "not-a-database"
    database.write_bytes(b"untouched")
    outcomes = acquisition.refresh_sources(
        ["not-registered"], start=date(2026, 1, 1), end=date(2026, 1, 2), db_path=database
    )
    assert isinstance(outcomes[0], acquisition.SourceRefreshFailure)
    assert database.read_bytes() == b"untouched"


def test_native_refresh_cli_reports_each_source_failure(tmp_path, monkeypatch, capsys):
    from baibai_engine.data_collection.transport.context import ProviderFetchStoppedError

    def fail(source, **kwargs):
        raise ProviderFetchStoppedError(f"{source.source_id} unavailable")

    monkeypatch.setattr(acquisition, "fetch_native_records", fail)
    status = economic_main(
        [
            "refresh",
            "mof-jgbcm-all",
            "fed-h15",
            "--start",
            "2026-07-20",
            "--end",
            "2026-07-20",
            "--db",
            str(tmp_path / "indicators.sqlite"),
        ]
    )
    assert status == 1
    report = capsys.readouterr().err
    assert "- mof-jgbcm-all: mof-jgbcm-all unavailable" in report
    assert "- fed-h15: fed-h15 unavailable" in report
    assert report.strip().splitlines()[-1] == (
        "error: refresh failed for 2 of 2 sources: mof-jgbcm-all, fed-h15"
    )


def test_tsr_all_history_collects_original_json_from_provider_floor(tmp_path, monkeypatch):
    from baibai_engine.data_collection.receipts.native_sources.documents import (
        SourceDocument,
        read_documents,
    )

    series = load_definitions().by_id()["jp.bankruptcies"]
    payload = '[{"period_division":"月次","title":"2003年1月 全国企業倒産 1,462件"}]'.encode()
    original = SourceDocument(
        series.source_id,
        "history",
        series.source_url,
        "application/json",
        datetime(2026, 7, 20, tzinfo=UTC),
        payload,
        {},
    )
    fetch = Mock(return_value=iter([original]))
    monkeypatch.setattr(acquisition, "fetch_source_documents", fetch)
    database = tmp_path / "indicators.sqlite"
    outcome = acquisition.refresh_sources(
        [series.source_id], db_path=database, start=None, end=date(2026, 7, 20)
    )[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    assert fetch.call_args.kwargs["start"] == date(2003, 1, 1)
    assert fetch.call_args.kwargs["force"] is True
    result = IndicatorsService(database).get_range(
        series.series_id, start=date(2003, 1, 1), end=date(2026, 7, 20)
    )
    assert result.observations[0].value == 1462
    with closing(sqlite3.connect(database)) as connection:
        assert next(read_documents(connection, series.source_id)).payload == payload


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1e99])
def test_invalid_l2_value_does_not_destroy_received_native_input(tmp_path, monkeypatch, value):
    day = date(2026, 7, 20)
    database = tmp_path / "indicators.sqlite"
    monkeypatch.setattr(acquisition, "fetch_native_records", lambda *a, **k: _mof_rows(day, value))
    source_id = load_definitions().by_id()["jp.10y"].source_id
    outcome = acquisition.refresh_sources([source_id], db_path=database, start=day, end=day)[0]
    assert isinstance(outcome, acquisition.SourceRefreshSuccess)
    with pytest.raises(ValueError, match=r"non-finite|plausible range"):
        IndicatorsService(database).get_range("jp.10y", start=day, end=day)
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM economic_source_records").fetchone()[0] == 1
        assert conn.execute("SELECT status, record_count FROM economic_source_runs").fetchall() == [
            ("ok", 1)
        ]


@pytest.mark.parametrize("value", [-2.0, 20.0])
def test_l2_accepts_the_declared_plausible_boundaries(value):
    from dataclasses import replace

    from baibai_engine.machine_analysis.observations.economic_indicators.validation import (
        reading_observation,
    )

    series = replace(load_definitions().by_id()["us.10y"], plausible_min=-2.0, plausible_max=20.0)
    result = reading_observation(
        series, observed_at=date(2026, 7, 20), value=value, vintage_at=None
    )
    assert result.value == value
    assert result.unit == series.unit
