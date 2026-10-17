from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from tests.engine.market_context.indicators.indicator_fixtures import (
    _write_observation,
)
from tests.helpers.indicator_store import (
    observation,
    store_h15_observations,
    store_mof_observations,
)

from baibai_engine.collection.economic_providers import (
    IndicatorsProviderError,
)
from baibai_engine.market_context.indicator_cli import (
    build_parser,
    main,
    parse_refresh_failure_count,
)
from baibai_engine.market_context.indicator_service import (
    IndicatorsService,
    RefreshFailure,
    RefreshSuccess,
)
from baibai_engine.market_context.indicators.db import (
    initialize_database,
    insert_observations,
    open_connection,
)
from baibai_engine.market_context.indicators.definitions import (
    load_definitions,
)
from baibai_engine.market_context.indicators.read_contracts import native_source_key
from baibai_engine.market_context.indicators.source_snapshots import store_snapshot


class IndicatorsServiceTests(unittest.TestCase):
    def test_derived_values_use_the_registry_bounds_at_the_calculation_boundary(self) -> None:
        for series_id, inputs in (
            ("jp.policy_path_gap", {"jp.2y": 12.0, "jp.policy_rate": 0.0}),
            ("us.erp", {"us.sp500_earnings_yield": 99.0, "us.10y": 4.0}),
        ):
            with self.subTest(series_id=series_id), tempfile.TemporaryDirectory() as tmp:
                database = Path(tmp) / "indicators.sqlite"
                with closing(initialize_database(database)) as conn:
                    insert_observations(
                        conn,
                        [
                            observation(input_id, date(2026, 7, 1), value)
                            for input_id, value in inputs.items()
                            if input_id
                            not in {"us.10y", "us.sp500_earnings_yield", "jp.policy_rate", "jp.2y"}
                        ],
                    )
                    if "jp.2y" in inputs:
                        store_mof_observations(
                            conn, [observation("jp.2y", date(2026, 7, 1), inputs["jp.2y"])]
                        )
                    if "jp.policy_rate" in inputs:
                        policy = load_definitions().by_id()["jp.policy_rate"]
                        store_snapshot(
                            conn,
                            source_key=native_source_key(policy),
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

                with self.assertRaisesRegex(ValueError, "outside plausible range"):
                    IndicatorsService(database).get_range(
                        series_id,
                        start=date(2026, 7, 1),
                        end=date(2026, 7, 31),
                        refresh=True,
                    )

                with closing(sqlite3.connect(database)) as conn:
                    stored = conn.execute(
                        "SELECT COUNT(*) FROM observations WHERE series_id = ?", (series_id,)
                    ).fetchone()[0]
                self.assertEqual(stored, 0)

    def test_refresh_all_history_uses_the_jquants_rolling_window(self) -> None:
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
            with (
                patch(
                    "baibai_engine.market_context.indicator_service._today_jst",
                    return_value=date(2026, 7, 20),
                ),
                patch(
                    "baibai_engine.market_context.indicator_service.fetch_native_records",
                    return_value=rows,
                ) as fetch,
            ):
                result = IndicatorsService(database).refresh_all_history(
                    "jp.foreign_flows", end=date(2026, 1, 1)
                )
            self.assertEqual(fetch.call_args.kwargs["start"], date(2016, 7, 20))
            self.assertEqual(
                [(item.observed_at, item.value) for item in result.observations],
                [(date(2025, 12, 26), 1)],
            )

    def test_refresh_all_history_uses_boj_timeseries_floor(self) -> None:
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
            with patch(
                "baibai_engine.market_context.indicator_service.fetch_native_records",
                return_value=records,
            ) as fetch:
                result = IndicatorsService(database).refresh_all_history(
                    "jp.policy_rate",
                    end=date(2026, 7, 20),
                )
            self.assertEqual(fetch.call_args.kwargs["start"], date(1998, 1, 1))
            self.assertEqual(
                [(row.observed_at, row.value) for row in result.observations],
                [(date(1998, 1, 5), 0.49)],
            )

    def test_parse_refresh_failure_count_is_none_when_no_roll_up_was_reported(self) -> None:
        self.assertIsNone(parse_refresh_failure_count(""))
        self.assertIsNone(parse_refresh_failure_count("Traceback (most recent call last):\n"))

    def test_refresh_cli_requires_one_range_mode(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["refresh", "us.10y", "--all-history", "--end", "2026-07-20"])
        self.assertTrue(args.all_history)
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "refresh",
                    "us.10y",
                    "--start",
                    "2026-01-01",
                    "--all-history",
                    "--end",
                    "2026-07-20",
                ]
            )

    def test_cli_handles_bad_db_path_without_traceback(self) -> None:
        self.assertEqual(main(["get", "us.10y", "--latest", "--db", "/tmp"]), 1)

    def test_cli_unknown_series_returns_controlled_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "indicators.sqlite"

            self.assertEqual(main(["get", "jp.cpi.stale", "--latest", "--db", str(db)]), 1)


def test_derived_read_ignores_old_materialization_and_does_not_fetch(tmp_path):
    database = tmp_path / "indicators.sqlite"
    _write_observation(database, "us.erp", observed_at=date(2026, 1, 2), value=-0.5)
    _store_multpl_earnings_yield(database, date(2026, 1, 1), "5.0")
    with closing(open_connection(database)) as connection:
        store_h15_observations(
            connection,
            [observation("us.10y", date(2026, 1, 30), 4.0, datetime(2026, 2, 1, tzinfo=UTC))],
        )
        connection.commit()
    with (
        patch(
            "baibai_engine.market_context.indicator_service.fetch_native_records",
            side_effect=AssertionError("derived read must not acquire native data"),
        ),
    ):
        service = IndicatorsService(database)
        ordinary = service.get_range("us.erp", start=date(2026, 1, 15), end=date(2026, 1, 31))
        refreshed = service.refresh_all_history("us.erp", end=date(2026, 1, 31))
        with patch(
            "baibai_engine.market_context.indicator_service._today_jst",
            return_value=date(2026, 1, 31),
        ):
            latest = service.get_latest("us.erp")
    assert [(row.observed_at, row.value) for row in ordinary.observations] == [
        (date(2026, 1, 30), 1.0)
    ]
    assert refreshed.observations == ordinary.observations
    assert latest.observations == ordinary.observations[-1:]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT observed_at, value FROM observations WHERE series_id = 'us.erp'"
        ).fetchall() == [("2026-01-02", -0.5)]
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM provider_runs WHERE series_id = 'us.erp'"
            ).fetchone()[0]
            == 0
        )


def test_old_derived_values_do_not_fill_missing_current_inputs(tmp_path):
    database = tmp_path / "indicators.sqlite"
    _write_observation(database, "us.erp", observed_at=date(2020, 1, 2), value=-0.5)
    _write_observation(database, "us.erp", observed_at=date(2025, 1, 2), value=-0.6)
    result = IndicatorsService(database).refresh_all_history("us.erp", end=date(2026, 6, 30))
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
            "baibai_engine.market_context.indicator_service.fetch_native_records",
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
    assert "\tderived\tcomputed" in capsys.readouterr().out


def _store_multpl_earnings_yield(database: Path, observed_at: date, value_text: str) -> None:
    """Seed the native table row read by the derived ERP tests, not an obsolete scalar."""
    series = load_definitions().by_id()["us.sp500_earnings_yield"]
    with closing(open_connection(database)) as conn:
        store_snapshot(
            conn,
            source_key=native_source_key(series),
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
    from baibai_engine.market_context import indicator_service as service_module

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

    monkeypatch.setattr(service_module, "fetch_native_records", fetch)
    sleep = Mock()
    monkeypatch.setattr(service_module.time, "sleep", sleep)
    result = IndicatorsService(database).get_range("jp.10y", start=day, end=day, refresh=True)
    assert [item.value for item in result.observations] == [1.5]
    assert len(contexts) == 2
    sleep.assert_called_once()
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT status, record_count FROM provider_runs").fetchall() == [
            ("ok", 1)
        ]


def test_refresh_continues_other_sources_and_shares_transport_context(tmp_path, monkeypatch):
    from baibai_engine.market_context import indicator_service as service_module

    database = tmp_path / "indicators.sqlite"
    day = date(2026, 7, 20)
    contexts = []

    def fetch(source, **kwargs):
        contexts.append(kwargs["context"])
        if source.provider == "mof_jgb":
            raise RuntimeError("source parser changed")
        return {day: [{"row": {"RIFLGFCY10_N.B": "4.2"}}]}

    monkeypatch.setattr(service_module, "fetch_native_records", fetch)
    outcomes = IndicatorsService(database).refresh_series(["jp.10y", "us.10y"], start=day, end=day)
    assert isinstance(outcomes[0], RefreshFailure)
    assert outcomes[0].message == "source parser changed"
    assert isinstance(outcomes[1], RefreshSuccess)
    assert outcomes[1].result.observations[0].value == 4.2
    assert len(contexts) == 2
    assert contexts[0] is contexts[1]


def test_native_revisions_are_idempotent_and_survive_a_later_source_failure(tmp_path, monkeypatch):
    from baibai_engine.collection.economic_providers.base import ProviderFetchStoppedError
    from baibai_engine.market_context import indicator_service as service_module

    database = tmp_path / "indicators.sqlite"
    day = date(2026, 7, 20)
    received = iter([_mof_rows(day, 1.5), _mof_rows(day, 1.5), _mof_rows(day, 1.6)])
    monkeypatch.setattr(service_module, "fetch_native_records", lambda *a, **k: next(received))
    service = IndicatorsService(database)
    first = service.get_range("jp.10y", start=day, end=day, refresh=True)
    same = service.get_range("jp.10y", start=day, end=day, refresh=True)
    revised = service.get_range("jp.10y", start=day, end=day, refresh=True)
    assert first.observations == same.observations
    assert revised.observations[0].value == 1.6

    def fail(*args, **kwargs):
        raise ProviderFetchStoppedError("source unavailable")

    monkeypatch.setattr(service_module, "fetch_native_records", fail)
    with pytest.raises(IndicatorsProviderError, match="source unavailable"):
        service.get_range("jp.10y", start=day, end=day, refresh=True)
    assert service.get_range("jp.10y", start=day, end=day).observations == revised.observations
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM indicator_source_snapshots").fetchone()[0] == 2


def test_latest_read_does_not_reacquire_stale_inputs(tmp_path, monkeypatch):
    from baibai_engine.market_context import indicator_service as service_module

    today = date(2026, 7, 27)
    stale = today - timedelta(days=8)
    replies = iter([_mof_rows(stale, 1.5), _mof_rows(today, 1.6)])
    calls = []

    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return next(replies)

    monkeypatch.setattr(service_module, "fetch_native_records", fetch)
    monkeypatch.setattr(service_module, "_today_jst", lambda: today)
    service = IndicatorsService(tmp_path / "indicators.sqlite")
    first = service.get_latest("jp.10y", refresh=True)
    cached = service.get_latest("jp.10y")
    assert first.observations == cached.observations
    assert cached.observations[0].observed_at == stale
    assert len(calls) == 1
    fresh = service.get_latest("jp.10y", refresh=True)
    assert fresh.observations[0].observed_at == today
    assert len(calls) == 2


def test_unknown_refresh_does_not_open_or_modify_the_store(tmp_path):
    database = tmp_path / "not-a-database"
    database.write_bytes(b"untouched")
    outcomes = IndicatorsService(database).refresh_series(
        ["not-registered"], start=date(2026, 1, 1), end=date(2026, 1, 2)
    )
    assert isinstance(outcomes[0], RefreshFailure)
    assert database.read_bytes() == b"untouched"


def test_native_refresh_cli_reports_each_source_failure(tmp_path, monkeypatch, capsys):
    from baibai_engine.collection.economic_providers.base import ProviderFetchStoppedError
    from baibai_engine.market_context import indicator_service as service_module

    def fail(source, **kwargs):
        raise ProviderFetchStoppedError(f"{source.source_id} unavailable")

    monkeypatch.setattr(service_module, "fetch_native_records", fail)
    status = main(
        [
            "refresh",
            "jp.10y",
            "us.10y",
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
    assert parse_refresh_failure_count(report) == 2
    assert (
        report.strip().splitlines()[-1] == "error: refresh failed for 2 of 2 series: jp.10y, us.10y"
    )


def test_tsr_all_history_collects_original_json_from_provider_floor(tmp_path, monkeypatch):
    from baibai_engine.collection.economic_documents import SourceDocument
    from baibai_engine.market_context import indicator_service as service_module

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
    monkeypatch.setattr(service_module, "fetch_source_documents", fetch)
    result = IndicatorsService(tmp_path / "indicators.sqlite").refresh_all_history(
        series.series_id, end=date(2026, 7, 20)
    )
    assert fetch.call_args.kwargs["start"] == date(2003, 1, 1)
    assert fetch.call_args.kwargs["force"] is True
    assert result.observations[0].value == 1462


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1e99])
def test_invalid_l2_value_does_not_destroy_received_native_input(tmp_path, monkeypatch, value):
    from baibai_engine.market_context import indicator_service as service_module

    day = date(2026, 7, 20)
    database = tmp_path / "indicators.sqlite"
    monkeypatch.setattr(
        service_module, "fetch_native_records", lambda *a, **k: _mof_rows(day, value)
    )
    with pytest.raises(ValueError, match=r"non-finite|plausible range"):
        IndicatorsService(database).get_range("jp.10y", start=day, end=day, refresh=True)
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM indicator_source_snapshots").fetchone()[0] == 1
        assert conn.execute("SELECT status, record_count FROM provider_runs").fetchall() == [
            ("ok", 1)
        ]


@pytest.mark.parametrize("value", [-2.0, 20.0])
def test_l2_accepts_the_declared_plausible_boundaries(value):
    from dataclasses import replace

    from baibai_engine.market_context.reading.observations import reading_observation

    series = replace(load_definitions().by_id()["us.10y"], plausible_min=-2.0, plausible_max=20.0)
    result = reading_observation(
        series, observed_at=date(2026, 7, 20), value=value, vintage_at=None
    )
    assert result.value == value
    assert result.unit == series.unit
