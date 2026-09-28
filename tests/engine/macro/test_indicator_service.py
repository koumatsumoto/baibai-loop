from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tests.engine.macro.indicator_fixtures import (
    _retired_counts,
    _series,
    _write_observation,
    _write_observation_with_coverage,
    _write_retired_series,
)
from tests.helpers.indicator_store import (
    observation,
)

from baibai_engine.macro.indicators.cli import (
    build_parser,
    main,
    parse_refresh_failure_count,
)
from baibai_engine.macro.indicators.db import (
    ObservationRecord,
    initialize_database,
    insert_observations,
    open_connection,
)
from baibai_engine.macro.indicators.definitions import (
    IndicatorDefinitions,
    SeriesDefinition,
    load_definitions,
)
from baibai_engine.macro.indicators.providers import (
    IndicatorsProviderError,
)
from baibai_engine.macro.indicators.providers.base import (
    FetchContext,
)
from baibai_engine.macro.indicators.service import (
    IndicatorsService,
    RefreshFailure,
    RefreshSuccess,
)


class IndicatorsServiceTests(unittest.TestCase):
    def test_refresh_rejects_observation_for_another_registered_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            requested = _series(
                "fred_csv",
                "TEST",
                plausible_min=-2.0,
                plausible_max=20.0,
            )
            other = _series(
                "fred_csv",
                "OTHER",
                series_id="other.series",
                plausible_min=-2.0,
                plausible_max=20.0,
            )
            definitions = IndicatorDefinitions(series=(requested, other))
            initialize_database(database, definitions=definitions).close()

            with (
                patch(
                    "baibai_engine.macro.indicators.service.load_definitions",
                    return_value=definitions,
                ),
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[observation("other.series", date(2026, 7, 20), 4.2)],
                ),
            ):
                outcomes = IndicatorsService(database).refresh_series(
                    ["test.series"],
                    start=date(2026, 7, 1),
                    end=date(2026, 7, 20),
                )

            with sqlite3.connect(database) as conn:
                stored = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
                run = conn.execute(
                    "SELECT series_id, status, record_count, error_message FROM provider_runs"
                ).fetchone()

            self.assertIsInstance(outcomes[0], RefreshFailure)
            self.assertEqual(stored, 0)
            self.assertEqual(run[0:3], ("test.series", "failed", 0))
            self.assertIn("provider returned observation for other.series", run[3])

    def test_refresh_rejects_observation_with_wrong_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            definition = _series(
                "fred_csv",
                "TEST",
                plausible_min=-2.0,
                plausible_max=20.0,
            )
            definitions = IndicatorDefinitions(series=(definition,))
            initialize_database(database, definitions=definitions).close()
            observation = ObservationRecord(
                series_id="test.series",
                observed_at=date(2026, 7, 20),
                value=4.2,
                unit="basis-points",
                source_url="https://example.com/data.csv",
                vintage_at=datetime.now(UTC),
            )

            with (
                patch(
                    "baibai_engine.macro.indicators.service.load_definitions",
                    return_value=definitions,
                ),
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[observation],
                ),
            ):
                outcomes = IndicatorsService(database).refresh_series(
                    ["test.series"],
                    start=date(2026, 7, 1),
                    end=date(2026, 7, 20),
                )

            with sqlite3.connect(database) as conn:
                stored = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
                run = conn.execute(
                    "SELECT status, record_count, error_message FROM provider_runs"
                ).fetchone()

            self.assertIsInstance(outcomes[0], RefreshFailure)
            self.assertEqual(stored, 0)
            self.assertEqual(run[0:2], ("failed", 0))
            self.assertIn("provider returned unit 'basis-points'; expected 'percent'", run[2])

    def test_refresh_rejects_one_out_of_range_value_without_partial_insert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            definition = _series(
                "fred_csv",
                "TEST",
                plausible_min=-2.0,
                plausible_max=20.0,
            )
            definitions = IndicatorDefinitions(series=(definition,))
            initialize_database(database, definitions=definitions).close()
            observations = [
                observation("test.series", date(2026, 7, 19), 4.2),
                observation("test.series", date(2026, 7, 20), 2000.0),
            ]

            with (
                patch(
                    "baibai_engine.macro.indicators.service.load_definitions",
                    return_value=definitions,
                ),
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=observations,
                ),
            ):
                outcomes = IndicatorsService(database).refresh_series(
                    ["test.series"],
                    start=date(2026, 7, 1),
                    end=date(2026, 7, 20),
                )

            conn = sqlite3.connect(database)
            try:
                stored = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
                run = conn.execute(
                    "SELECT status, record_count, error_message FROM provider_runs"
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(len(outcomes), 1)
            failure = outcomes[0]
            self.assertIsInstance(failure, RefreshFailure)
            assert isinstance(failure, RefreshFailure)
            self.assertIn("outside plausible range [-2, 20]", failure.message)
            self.assertEqual(stored, 0)
            self.assertEqual(run[0:2], ("failed", 0))
            self.assertIn("value 2000", run[2])

    def test_refresh_without_plausible_range_keeps_backward_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            definition = _series("fred_csv", "TEST")
            definitions = IndicatorDefinitions(series=(definition,))
            initialize_database(database, definitions=definitions).close()

            with (
                patch(
                    "baibai_engine.macro.indicators.service.load_definitions",
                    return_value=definitions,
                ),
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[observation("test.series", date(2026, 7, 20), 1e100)],
                ),
            ):
                outcomes = IndicatorsService(database).refresh_series(
                    ["test.series"],
                    start=date(2026, 7, 1),
                    end=date(2026, 7, 20),
                )

            self.assertIsInstance(outcomes[0], RefreshSuccess)
            conn = sqlite3.connect(database)
            try:
                stored = conn.execute("SELECT value FROM observations").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(stored, 1e100)

    def test_refresh_accepts_values_on_both_plausible_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            definition = _series(
                "fred_csv",
                "TEST",
                plausible_min=-2.0,
                plausible_max=20.0,
            )
            definitions = IndicatorDefinitions(series=(definition,))
            initialize_database(database, definitions=definitions).close()

            with (
                patch(
                    "baibai_engine.macro.indicators.service.load_definitions",
                    return_value=definitions,
                ),
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[
                        observation("test.series", date(2026, 7, 19), -2.0),
                        observation("test.series", date(2026, 7, 20), 20.0),
                    ],
                ),
            ):
                outcomes = IndicatorsService(database).refresh_series(
                    ["test.series"],
                    start=date(2026, 7, 1),
                    end=date(2026, 7, 20),
                )

            self.assertIsInstance(outcomes[0], RefreshSuccess)
            conn = sqlite3.connect(database)
            try:
                stored = conn.execute(
                    "SELECT value FROM observations ORDER BY observed_at"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(stored, [(-2.0,), (20.0,)])

    def test_refresh_all_history_uses_provider_floor_and_forces_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            _write_observation(
                database,
                "us.fed_funds.upper",
                observed_at=date(1950, 1, 1),
                value=1.0,
            )
            observation = ObservationRecord(
                series_id="us.fed_funds.upper",
                observed_at=date(1954, 7, 1),
                value=1.13,
                unit="percent",
                source_url="https://example.com/data.csv",
                vintage_at=datetime.now(UTC),
            )
            with patch(
                "baibai_engine.macro.indicators.service.fetch_observations",
                return_value=[observation],
            ) as fetch:
                result = IndicatorsService(database).refresh_all_history(
                    "us.fed_funds.upper",
                    end=date(2026, 7, 20),
                )

            self.assertEqual(result.observations, (observation,))
            self.assertEqual(fetch.call_args.kwargs["start"], date(1900, 1, 1))
            self.assertEqual(fetch.call_args.kwargs["end"], date(2026, 7, 20))
            conn = open_connection(database)
            try:
                first = conn.execute(
                    "SELECT MIN(observed_at) FROM observations WHERE series_id = ?",
                    ("us.fed_funds.upper",),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(first, "1954-07-01")

    def test_failed_full_history_refresh_preserves_existing_observations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            _write_observation(
                database,
                "us.fed_funds.upper",
                observed_at=date(1950, 1, 1),
                value=1.0,
            )

            with (
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    side_effect=IndicatorsProviderError("unavailable"),
                ),
                patch("baibai_engine.macro.indicators.service.time.sleep"),
                self.assertRaisesRegex(IndicatorsProviderError, "unavailable"),
            ):
                IndicatorsService(database).refresh_all_history(
                    "us.fed_funds.upper",
                    end=date(2026, 7, 20),
                )

            conn = open_connection(database)
            try:
                first = conn.execute(
                    "SELECT MIN(observed_at) FROM observations WHERE series_id = ?",
                    ("us.fed_funds.upper",),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(first, "1950-01-01")

    def test_derived_full_history_refresh_replaces_the_previous_alignment_grid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            _write_observation(
                database,
                "us.erp",
                observed_at=date(2026, 1, 2),
                value=-0.5,
                vintage_at=datetime(2026, 7, 26, tzinfo=UTC),
            )
            definition = load_definitions().by_id()["us.erp"]
            rebuilt = ObservationRecord(
                series_id="us.erp",
                observed_at=date(2026, 1, 30),
                value=-0.4,
                unit=definition.unit,
                source_url=definition.source_url,
                vintage_at=datetime.now(UTC),
            )

            with patch(
                "baibai_engine.macro.indicators.service.fetch_observations",
                return_value=[rebuilt],
            ):
                IndicatorsService(database).refresh_all_history(
                    "us.erp",
                    end=date(2026, 6, 30),
                )

            conn = open_connection(database)
            try:
                rows = conn.execute(
                    "SELECT observed_at, value FROM observations WHERE series_id = ? "
                    "ORDER BY observed_at",
                    ("us.erp",),
                ).fetchall()
            finally:
                conn.close()

            self.assertEqual(
                [(row["observed_at"], row["value"]) for row in rows],
                [("2026-01-30", -0.4)],
            )

    def test_derived_full_history_refresh_rejects_period_coverage_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            for observed_at, value in (
                (date(2020, 1, 2), -0.5),
                (date(2025, 1, 2), -0.6),
            ):
                _write_observation(database, "us.erp", observed_at=observed_at, value=value)
            definition = load_definitions().by_id()["us.erp"]
            partial = ObservationRecord(
                series_id="us.erp",
                observed_at=date(2026, 1, 30),
                value=-0.4,
                unit=definition.unit,
                source_url=definition.source_url,
                vintage_at=datetime(2026, 2, 1, tzinfo=UTC),
            )

            with (
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[partial],
                ),
                self.assertRaisesRegex(IndicatorsProviderError, "would drop 2 existing monthly"),
            ):
                IndicatorsService(database).refresh_all_history(
                    "us.erp",
                    end=date(2026, 6, 30),
                )

            conn = open_connection(database)
            try:
                rows = conn.execute(
                    "SELECT observed_at, value FROM observations WHERE series_id = ? "
                    "ORDER BY observed_at",
                    ("us.erp",),
                ).fetchall()
                latest_run = conn.execute(
                    "SELECT status FROM provider_runs WHERE series_id = ? "
                    "ORDER BY finished_at DESC LIMIT 1",
                    ("us.erp",),
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(
                [(row["observed_at"], row["value"]) for row in rows],
                [("2020-01-02", -0.5), ("2025-01-02", -0.6)],
            )
            assert latest_run is not None
            self.assertEqual(latest_run["status"], "failed")

    def test_repeated_range_refresh_is_idempotent_for_unchanged_data(self) -> None:
        # Mirrors the daily batch re-running the same rolling window: a provider
        # stamps a fresh now() vintage on every fetch, yet the store must
        # converge to one vintage per observed_at when the values are unchanged.
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            service = IndicatorsService(database)

            def _refetch(*_args: object, **_kwargs: object) -> list[ObservationRecord]:
                return [
                    ObservationRecord(
                        series_id="us.10y",
                        observed_at=date(2026, 5, day),
                        value=value,
                        unit="percent",
                        source_url="https://example.com/us10y.csv",
                        vintage_at=datetime.now(UTC),
                    )
                    for day, value in ((1, 4.39), (2, 4.41))
                ]

            with patch(
                "baibai_engine.macro.indicators.service.fetch_observations",
                side_effect=_refetch,
            ):
                for _ in range(3):
                    service.get_range(
                        "us.10y", start=date(2026, 5, 1), end=date(2026, 5, 2), refresh=True
                    )

            conn = open_connection(database)
            try:
                rows = conn.execute(
                    "SELECT observed_at, value FROM observations WHERE series_id = ? "
                    "ORDER BY observed_at, vintage_at",
                    ("us.10y",),
                ).fetchall()
            finally:
                conn.close()
            # Three identical refreshes leave exactly one row per observed_at.
            self.assertEqual(
                [(row["observed_at"], row["value"]) for row in rows],
                [("2026-05-01", 4.39), ("2026-05-02", 4.41)],
            )

    def test_range_refresh_adds_a_vintage_only_when_the_source_revises_a_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            service = IndicatorsService(database)
            common = {
                "series_id": "us.10y",
                "observed_at": date(2026, 5, 1),
                "unit": "percent",
                "source_url": "https://example.com/us10y.csv",
            }
            first = [
                ObservationRecord(**common, value=4.39, vintage_at=datetime(2026, 5, 2, tzinfo=UTC))
            ]
            revised = [
                ObservationRecord(**common, value=4.55, vintage_at=datetime(2026, 5, 9, tzinfo=UTC))
            ]
            with patch(
                "baibai_engine.macro.indicators.service.fetch_observations",
                side_effect=[first, revised],
            ):
                service.get_range(
                    "us.10y", start=date(2026, 5, 1), end=date(2026, 5, 1), refresh=True
                )
                result = service.get_range(
                    "us.10y", start=date(2026, 5, 1), end=date(2026, 5, 1), refresh=True
                )

            conn = open_connection(database)
            try:
                rows = conn.execute(
                    "SELECT value, vintage_at FROM observations WHERE series_id = ? "
                    "AND observed_at = ? ORDER BY vintage_at",
                    ("us.10y", "2026-05-01"),
                ).fetchall()
            finally:
                conn.close()
            # The revision keeps both vintages; point-in-time read returns the latest.
            self.assertEqual(
                [(row["value"], row["vintage_at"]) for row in rows],
                [
                    (4.39, "2026-05-02T00:00:00+00:00"),
                    (4.55, "2026-05-09T00:00:00+00:00"),
                ],
            )
            self.assertEqual(result.observations[-1].value, 4.55)

    def test_refresh_all_history_uses_the_jquants_rolling_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            conn = initialize_database(database)
            try:
                insert_observations(
                    conn,
                    [
                        ObservationRecord(
                            series_id="jp.foreign_flows",
                            observed_at=observed_at,
                            value=1.0,
                            unit="jpy-thousand",
                            source_url="https://jpx-jquants.com/ja/spec/eq-investor-types",
                            vintage_at=vintage_at,
                        )
                        for observed_at, vintage_at in (
                            (date(2021, 6, 1), datetime(2026, 1, 1, tzinfo=UTC)),
                            (date(2025, 1, 1), datetime(2026, 1, 1, tzinfo=UTC)),
                            (date(2025, 6, 1), datetime(2026, 2, 1, tzinfo=UTC)),
                        )
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            observation = ObservationRecord(
                series_id="jp.foreign_flows",
                observed_at=date(2025, 12, 26),
                value=1.0,
                unit="jpy-thousand",
                source_url="https://jpx-jquants.com/ja/spec/eq-investor-types",
                vintage_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            with (
                patch(
                    "baibai_engine.macro.indicators.service._today_jst",
                    return_value=date(2026, 7, 20),
                ),
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[observation],
                ) as fetch,
            ):
                IndicatorsService(database).refresh_all_history(
                    "jp.foreign_flows",
                    end=date(2026, 1, 1),
                )

            self.assertEqual(fetch.call_args.kwargs["start"], date(2016, 7, 20))
            conn = open_connection(database)
            try:
                observed_dates = [
                    row[0]
                    for row in conn.execute(
                        "SELECT observed_at FROM observations WHERE series_id = ? "
                        "ORDER BY observed_at",
                        ("jp.foreign_flows",),
                    )
                ]
            finally:
                conn.close()
            # 2025-06-01 carries a vintage later than the requested end, so the
            # point-in-time replacement leaves it alone; the rest of the window is
            # replaced by what the provider now returns.
            self.assertEqual(observed_dates, ["2025-06-01", "2025-12-26"])

    def test_refresh_all_history_uses_boj_timeseries_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            conn = initialize_database(database)
            try:
                insert_observations(
                    conn,
                    [
                        ObservationRecord(
                            series_id="jp.policy_rate",
                            observed_at=observed_at,
                            value=0.978,
                            unit="percent",
                            source_url="https://example.com/another-provider",
                            vintage_at=datetime(2026, 7, 10, tzinfo=UTC),
                        )
                        for observed_at in (date(2026, 7, 9), date(2027, 1, 5))
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            observation = ObservationRecord(
                series_id="jp.policy_rate",
                observed_at=date(1998, 1, 5),
                value=0.49,
                unit="percent",
                source_url="https://www.stat-search.boj.or.jp/api/v1/getDataCode",
                vintage_at=datetime.now(UTC),
            )
            with patch(
                "baibai_engine.macro.indicators.service.fetch_observations",
                return_value=[observation],
            ) as fetch:
                IndicatorsService(database).refresh_all_history(
                    "jp.policy_rate",
                    end=date(2026, 7, 20),
                )

            self.assertEqual(fetch.call_args.kwargs["start"], date(1998, 1, 1))
            conn = open_connection(database)
            try:
                stale_source_dates = [
                    row[0]
                    for row in conn.execute(
                        "SELECT observed_at FROM observations WHERE series_id = ? "
                        "AND source_url != ? ORDER BY observed_at",
                        ("jp.policy_rate", observation.source_url),
                    )
                ]
            finally:
                conn.close()
            self.assertEqual(stale_source_dates, ["2027-01-05"])

    def test_refresh_all_history_uses_tsr_bankruptcies_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            observation = ObservationRecord(
                series_id="jp.bankruptcies",
                observed_at=date(2003, 1, 1),
                value=1462,
                unit="count",
                source_url=("https://www.tsr-net.co.jp/news/status/json/search.json"),
                vintage_at=datetime.now(UTC),
            )
            with patch(
                "baibai_engine.macro.indicators.service.fetch_observations",
                return_value=[observation],
            ) as fetch:
                IndicatorsService(database).refresh_all_history(
                    "jp.bankruptcies",
                    end=date(2026, 7, 20),
                )

            self.assertEqual(fetch.call_args.kwargs["start"], date(2003, 1, 1))

    def test_empty_full_history_refresh_fails_and_preserves_existing_observations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            _write_observation(
                database,
                "us.fed_funds.upper",
                observed_at=date(1950, 1, 1),
                value=1.0,
            )

            with (
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[],
                ),
                self.assertRaisesRegex(IndicatorsProviderError, "returned no observations"),
            ):
                IndicatorsService(database).refresh_all_history(
                    "us.fed_funds.upper",
                    end=date(2026, 7, 20),
                )

            conn = open_connection(database)
            try:
                first = conn.execute(
                    "SELECT MIN(observed_at) FROM observations WHERE series_id = ?",
                    ("us.fed_funds.upper",),
                ).fetchone()[0]
                latest_status = conn.execute(
                    "SELECT status FROM provider_runs WHERE series_id = ? "
                    "ORDER BY finished_at DESC LIMIT 1",
                    ("us.fed_funds.upper",),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(first, "1950-01-01")
            self.assertEqual(latest_status, "failed")

    def test_refresh_series_isolates_a_failing_series_from_the_rest(self) -> None:
        # One broken source must not leave every other series in the group stale,
        # and every failure must be reported (not just the first).
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            initialize_database(database).close()

            def _fetch(
                series: SeriesDefinition,
                *,
                start: date,
                end: date,
                context: FetchContext | None,
            ) -> list[ObservationRecord]:
                if series.series_id == "us.2y":
                    raise IndicatorsProviderError("source unavailable")
                return [observation(series.series_id, date(2026, 7, 20), 4.2)]

            with (
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    side_effect=_fetch,
                ),
                patch("baibai_engine.macro.indicators.service.time.sleep"),
            ):
                outcomes = IndicatorsService(database).refresh_series(
                    ["us.10y", "us.2y", "us.30y"],
                    start=date(2026, 7, 1),
                    end=date(2026, 7, 20),
                )

            failures = [outcome for outcome in outcomes if isinstance(outcome, RefreshFailure)]
            successes = [outcome for outcome in outcomes if isinstance(outcome, RefreshSuccess)]
            self.assertEqual([failure.series_id for failure in failures], ["us.2y"])
            self.assertIn("source unavailable", failures[0].message)
            self.assertEqual([success.series_id for success in successes], ["us.10y", "us.30y"])
            conn = open_connection(database)
            try:
                stored = {
                    str(row["series_id"])
                    for row in conn.execute("SELECT DISTINCT series_id FROM observations")
                }
                failed_runs = {
                    str(row["series_id"])
                    for row in conn.execute(
                        "SELECT series_id FROM provider_runs WHERE status = 'failed'"
                    )
                }
            finally:
                conn.close()
            self.assertEqual(stored, {"us.10y", "us.30y"})
            self.assertEqual(failed_runs, {"us.2y"})

    def test_refresh_series_reports_an_unknown_series_without_stopping_the_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            initialize_database(database).close()

            with patch(
                "baibai_engine.macro.indicators.service.fetch_observations",
                side_effect=lambda series, **_: [
                    observation(series.series_id, date(2026, 7, 20), 4.2)
                ],
            ):
                outcomes = IndicatorsService(database).refresh_series(
                    ["jp.cpi.stale", "us.10y"],
                    start=date(2026, 7, 1),
                    end=date(2026, 7, 20),
                )

            self.assertIsInstance(outcomes[0], RefreshFailure)
            self.assertIsInstance(outcomes[1], RefreshSuccess)
            failure = outcomes[0]
            assert isinstance(failure, RefreshFailure)
            self.assertEqual(failure.message, "unknown indicator series: jp.cpi.stale")

    def test_unknown_only_refresh_does_not_prune_retired_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            _write_retired_series(database)

            outcomes = IndicatorsService(database).refresh_series(
                ["jp.cpi.stale"],
                start=date(2026, 7, 1),
                end=date(2026, 7, 20),
            )

            self.assertEqual(
                outcomes,
                [
                    RefreshFailure(
                        "jp.cpi.stale",
                        "unknown indicator series: jp.cpi.stale",
                    )
                ],
            )
            self.assertEqual(_retired_counts(database), (1, 1, 1))

    def test_refresh_series_shares_one_fetch_context_across_the_pass(self) -> None:
        # A shared context is what makes a bulk source file download once for every
        # series that maps to it and a browser-backed provider launch once.
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            initialize_database(database).close()
            cache_sizes: list[int] = []

            def _fetch(
                series: SeriesDefinition,
                *,
                start: date,
                end: date,
                context: FetchContext | None,
            ) -> list[ObservationRecord]:
                assert context is not None
                cache_sizes.append(len(context.bytes_cache))
                context.bytes_cache[(series.series_id, ())] = b"payload"
                return [observation(series.series_id, date(2026, 7, 20), 4.2)]

            with patch(
                "baibai_engine.macro.indicators.service.fetch_observations",
                side_effect=_fetch,
            ):
                IndicatorsService(database).refresh_series(
                    ["us.10y", "us.2y", "us.30y"],
                    start=date(2026, 7, 1),
                    end=date(2026, 7, 20),
                )

            self.assertEqual(cache_sizes, [0, 1, 2])

    def test_refresh_series_isolates_a_provider_error_it_does_not_recognize(self) -> None:
        # A provider can raise an unwrapped third-party error (a dead headless
        # browser does). It must stay one series' failure, not abort the pass.
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            initialize_database(database).close()

            def _fetch(
                series: SeriesDefinition,
                *,
                start: date,
                end: date,
                context: FetchContext | None,
            ) -> list[ObservationRecord]:
                if series.series_id == "us.2y":
                    raise RuntimeError("browser process died")
                return [observation(series.series_id, date(2026, 7, 20), 4.2)]

            with patch(
                "baibai_engine.macro.indicators.service.fetch_observations",
                side_effect=_fetch,
            ):
                outcomes = IndicatorsService(database).refresh_series(
                    ["us.10y", "us.2y", "us.30y"],
                    start=date(2026, 7, 1),
                    end=date(2026, 7, 20),
                )

            self.assertEqual(
                [outcome.series_id for outcome in outcomes if isinstance(outcome, RefreshSuccess)],
                ["us.10y", "us.30y"],
            )
            failure = outcomes[1]
            assert isinstance(failure, RefreshFailure)
            self.assertIn("browser process died", failure.message)

    def test_refresh_discards_cached_response_bytes_after_a_failure(self) -> None:
        # A source can answer HTTP 200 with a block page, which caches like data. If
        # it survived, the retry would re-read it and every later series sharing the
        # URL would inherit the failure.
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            initialize_database(database).close()
            cache_key = ("https://example.com/bulk.csv", ())
            cache_sizes: list[int] = []

            def _fetch(
                series: SeriesDefinition,
                *,
                start: date,
                end: date,
                context: FetchContext | None,
            ) -> list[ObservationRecord]:
                assert context is not None
                cache_sizes.append(len(context.bytes_cache))
                context.bytes_cache[cache_key] = b"<html>blocked</html>"
                if series.series_id == "us.10y":
                    raise IndicatorsProviderError("missing Time Period header")
                return [observation(series.series_id, date(2026, 7, 20), 4.2)]

            with (
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    side_effect=_fetch,
                ),
                patch("baibai_engine.macro.indicators.service.time.sleep"),
            ):
                outcomes = IndicatorsService(database).refresh_series(
                    ["us.10y", "us.2y"],
                    start=date(2026, 7, 1),
                    end=date(2026, 7, 20),
                )

            # attempt 1 and its retry both start from an empty cache, and the series
            # that follows the failure does too.
            self.assertEqual(cache_sizes, [0, 0, 0])
            self.assertIsInstance(outcomes[0], RefreshFailure)
            self.assertIsInstance(outcomes[1], RefreshSuccess)

    def test_refresh_rejects_a_non_finite_value_before_the_store(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                database = Path(tmp) / "macro.sqlite"
                initialize_database(database).close()

                with (
                    patch(
                        "baibai_engine.macro.indicators.service.fetch_observations",
                        return_value=[observation("us.10y", date(2026, 7, 20), value)],
                    ),
                    self.assertRaisesRegex(IndicatorsProviderError, "non-finite value"),
                ):
                    IndicatorsService(database).get_range(
                        "us.10y",
                        start=date(2026, 7, 1),
                        end=date(2026, 7, 20),
                        refresh=True,
                    )

                conn = open_connection(database)
                try:
                    stored = conn.execute(
                        "SELECT COUNT(*) FROM observations WHERE series_id = 'us.10y'"
                    ).fetchone()[0]
                    status = conn.execute(
                        "SELECT status FROM provider_runs WHERE series_id = 'us.10y' "
                        "ORDER BY finished_at DESC LIMIT 1"
                    ).fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(stored, 0)
                self.assertEqual(status, "failed")

    def test_refresh_cli_reports_every_failed_series_and_exits_non_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            initialize_database(database).close()
            errors = io.StringIO()

            def _fetch(
                series: SeriesDefinition,
                *,
                start: date,
                end: date,
                context: FetchContext | None,
            ) -> list[ObservationRecord]:
                if series.series_id in {"us.2y", "us.30y"}:
                    raise IndicatorsProviderError(f"{series.series_id} source unavailable")
                return [observation(series.series_id, date(2026, 7, 20), 4.2)]

            with (
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    side_effect=_fetch,
                ),
                patch("baibai_engine.macro.indicators.service.time.sleep"),
                redirect_stderr(errors),
            ):
                exit_code = main(
                    [
                        "refresh",
                        "us.10y",
                        "us.2y",
                        "us.30y",
                        "--start",
                        "2026-07-01",
                        "--end",
                        "2026-07-20",
                        "--db",
                        str(database),
                    ]
                )

            self.assertEqual(exit_code, 1)
            reported = errors.getvalue()
            self.assertIn("2 of 3 series failed to refresh", reported)
            self.assertIn("- us.2y: us.2y source unavailable", reported)
            self.assertIn("- us.30y: us.30y source unavailable", reported)
            # A log reader that keeps only the tail must still get the failed IDs.
            self.assertEqual(
                reported.strip().splitlines()[-1],
                "error: refresh failed for 2 of 3 series: us.2y, us.30y",
            )
            # A caller counting failures reads that same line, so the reader is
            # bound to what the CLI actually emits rather than to a copy of it.
            self.assertEqual(parse_refresh_failure_count(reported), 2)

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

    def test_get_range_normalizes_provider_order_to_ascending(self) -> None:
        # ECB FX (and any newest-first provider) returns observations descending;
        # get_range must normalize to ascending observed_at on the provider-fetch path.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            initialize_database(db).close()
            descending = [
                ObservationRecord(
                    series_id="us.10y",
                    observed_at=date(2026, 5, day),
                    value=value,
                    unit="percent",
                    source_url="https://example.com/data.csv",
                    vintage_at=datetime.now(UTC),
                )
                for day, value in ((3, 4.45), (2, 4.41), (1, 4.39))
            ]
            with patch(
                "baibai_engine.macro.indicators.service.fetch_observations",
                return_value=descending,
            ):
                result = IndicatorsService(db).get_range(
                    "us.10y",
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 3),
                    refresh=True,
                )

            observed_dates = [obs.observed_at for obs in result.observations]
            self.assertEqual(
                observed_dates,
                [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)],
            )

    def test_get_range_retries_transient_provider_error_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            initialize_database(db).close()
            observation = ObservationRecord(
                series_id="us.10y",
                observed_at=date(2026, 5, 1),
                value=4.39,
                unit="percent",
                source_url="https://example.com/data.csv",
                vintage_at=datetime.now(UTC),
            )
            with (
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    side_effect=[
                        IndicatorsProviderError("temporary upstream error"),
                        [observation],
                    ],
                ) as fetch,
                patch("baibai_engine.macro.indicators.service.time.sleep") as sleep,
            ):
                result = IndicatorsService(db).get_range(
                    "us.10y",
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                    refresh=True,
                )

            self.assertFalse(result.cache_hit)
            self.assertEqual(fetch.call_count, 2)
            sleep.assert_called_once()
            self.assertEqual(result.observations[0].value, 4.39)
            conn = sqlite3.connect(db)
            try:
                runs = conn.execute(
                    "SELECT status, record_count FROM provider_runs WHERE series_id = ?",
                    ("us.10y",),
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(runs, [("ok", 1)])

    def test_get_range_daily_partial_provider_run_does_not_cover_unobserved_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            initialize_database(db).close()
            first_observation = ObservationRecord(
                series_id="jp.10y",
                observed_at=date(2026, 7, 8),
                value=2.856,
                unit="percent",
                source_url="https://example.com/data.csv",
                vintage_at=datetime.now(UTC),
            )
            second_observation = ObservationRecord(
                series_id="jp.10y",
                observed_at=date(2026, 7, 9),
                value=2.858,
                unit="percent",
                source_url="https://example.com/data.csv",
                vintage_at=datetime.now(UTC),
            )
            with patch(
                "baibai_engine.macro.indicators.service.fetch_observations",
                side_effect=[[first_observation], [second_observation]],
            ) as fetch:
                first = IndicatorsService(db).get_range(
                    "jp.10y",
                    start=date(2026, 7, 8),
                    end=date(2026, 7, 9),
                    refresh=True,
                )
                second = IndicatorsService(db).get_range(
                    "jp.10y",
                    start=date(2026, 7, 9),
                    end=date(2026, 7, 9),
                )

            self.assertFalse(first.cache_hit)
            self.assertFalse(second.cache_hit)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(fetch.call_args.kwargs["start"], date(2026, 7, 9))
            self.assertEqual(second.observations[0].observed_at, date(2026, 7, 9))
            conn = sqlite3.connect(db)
            try:
                runs = conn.execute(
                    "SELECT range_start, range_end, record_count "
                    "FROM provider_runs WHERE series_id = ? ORDER BY range_start",
                    ("jp.10y",),
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(
                runs,
                [
                    ("2026-07-08", "2026-07-08", 1),
                    ("2026-07-09", "2026-07-09", 1),
                ],
            )

    def test_get_range_uses_cache_when_coverage_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            _write_observation_with_coverage(
                db,
                "us.10y",
                observed_at=date(2026, 5, 4),
                value=4.45,
                coverage_start=date(2026, 5, 1),
                coverage_end=date(2026, 5, 15),
            )

            result = IndicatorsService(db).get_range(
                "us.10y",
                start=date(2026, 5, 1),
                end=date(2026, 5, 15),
            )

            self.assertTrue(result.cache_hit)
            self.assertEqual(len(result.observations), 1)
            self.assertEqual(result.observations[0].value, 4.45)

    def test_get_latest_uses_fresh_cached_observation_before_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            today = date(2026, 7, 27)
            observed_at = today
            _write_observation(
                db,
                "us.10y",
                observed_at=observed_at,
                value=4.45,
            )

            with (
                patch(
                    "baibai_engine.macro.indicators.service._today_jst",
                    return_value=today,
                ),
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    side_effect=AssertionError("provider should not be called"),
                ),
            ):
                result = IndicatorsService(db).get_latest("us.10y")

            self.assertTrue(result.cache_hit)
            self.assertEqual(result.observations[0].observed_at, observed_at)

    def test_get_latest_uses_short_daily_provider_lookback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            initialize_database(db).close()
            today = date(2026, 7, 27)
            observation = ObservationRecord(
                series_id="jp.10y",
                observed_at=today,
                value=2.8,
                unit="percent",
                source_url="https://example.com/data.csv",
                vintage_at=datetime.now(UTC),
            )
            with (
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[observation],
                ) as fetch,
                patch(
                    "baibai_engine.macro.indicators.service.load_reading_rules",
                    side_effect=AssertionError("explicit refresh does not need cache rules"),
                ),
                patch(
                    "baibai_engine.macro.indicators.service._today_jst",
                    return_value=today,
                ),
            ):
                result = IndicatorsService(db).get_latest("jp.10y", refresh=True)

            self.assertFalse(result.cache_hit)
            self.assertEqual(fetch.call_args.kwargs["start"], today - timedelta(days=14))

    def test_get_latest_refreshes_stale_latest_even_when_range_has_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            today = date(2026, 7, 27)
            _write_observation_with_coverage(
                db,
                "jp.10y",
                observed_at=today - timedelta(days=8),
                value=2.7,
                coverage_start=today - timedelta(days=14),
                coverage_end=today,
            )
            observation = ObservationRecord(
                series_id="jp.10y",
                observed_at=today - timedelta(days=1),
                value=2.8,
                unit="percent",
                source_url="https://example.com/data.csv",
                vintage_at=datetime.now(UTC),
            )
            with (
                patch(
                    "baibai_engine.macro.indicators.service._today_jst",
                    return_value=today,
                ),
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[observation],
                ) as fetch,
            ):
                result = IndicatorsService(db).get_latest("jp.10y")

            self.assertFalse(result.cache_hit)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(result.observations[0].observed_at, today - timedelta(days=1))
            self.assertEqual(result.observations[0].value, 2.8)

    def test_get_latest_uses_series_staleness_override_for_consumer_sentiment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            today = date(2026, 7, 27)
            definition = load_definitions().by_id()["us.consumer_sentiment"]
            _write_observation_with_coverage(
                database,
                definition.series_id,
                observed_at=today - timedelta(days=66),
                value=50.0,
                coverage_start=today - timedelta(days=370),
                coverage_end=today,
            )
            current = ObservationRecord(
                series_id=definition.series_id,
                observed_at=today - timedelta(days=28),
                value=51.0,
                unit=definition.unit,
                source_url=definition.source_url,
                vintage_at=datetime.now(UTC),
            )

            with (
                patch(
                    "baibai_engine.macro.indicators.service._today_jst",
                    return_value=today,
                ),
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[current],
                ) as fetch,
            ):
                result = IndicatorsService(database).get_latest(definition.series_id)

            self.assertFalse(result.cache_hit)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(result.observations[0].value, 51.0)

    def test_get_latest_accepts_cache_at_series_staleness_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            today = date(2026, 7, 27)
            _write_observation(
                database,
                "us.consumer_sentiment",
                observed_at=today - timedelta(days=65),
                value=50.0,
            )

            with (
                patch(
                    "baibai_engine.macro.indicators.service._today_jst",
                    return_value=today,
                ),
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    side_effect=AssertionError("provider should not be called"),
                ),
            ):
                result = IndicatorsService(database).get_latest("us.consumer_sentiment")

            self.assertTrue(result.cache_hit)

    def test_multpl_latest_uses_reading_staleness_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            today = date(2026, 7, 27)
            _write_observation(
                database,
                "us.sp500_cape",
                observed_at=today - timedelta(days=8),
                value=39.0,
            )
            definition = load_definitions().by_id()["us.sp500_cape"]
            current = ObservationRecord(
                series_id=definition.series_id,
                observed_at=today,
                value=39.5,
                unit=definition.unit,
                source_url=definition.source_url,
                vintage_at=datetime.now(UTC),
            )

            with (
                patch(
                    "baibai_engine.macro.indicators.service._today_jst",
                    return_value=today,
                ),
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[current],
                ) as fetch,
            ):
                result = IndicatorsService(database).get_latest("us.sp500_cape")

            self.assertEqual(definition.frequency, "daily")
            self.assertFalse(result.cache_hit)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(result.observations[0].value, 39.5)

    def test_cli_search_and_cached_get(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            _write_observation_with_coverage(
                db,
                "us.10y",
                observed_at=date(2026, 5, 4),
                value=4.45,
                coverage_start=date(2026, 5, 1),
                coverage_end=date(2026, 5, 15),
            )

            self.assertEqual(main(["search", "CPI"]), 0)
            self.assertEqual(
                main(
                    [
                        "get",
                        "us.10y",
                        "--start",
                        "2026-05-01",
                        "--end",
                        "2026-05-15",
                        "--db",
                        str(db),
                    ]
                ),
                0,
            )

    def test_cli_handles_bad_db_path_without_traceback(self) -> None:
        self.assertEqual(main(["get", "us.10y", "--latest", "--db", "/tmp"]), 1)

    def test_cli_unknown_series_returns_controlled_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"

            self.assertEqual(main(["get", "jp.cpi.stale", "--latest", "--db", str(db)]), 1)
