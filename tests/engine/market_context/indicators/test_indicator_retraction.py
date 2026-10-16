from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, date, datetime
from pathlib import Path

from tests.helpers.indicator_store import (
    downgrade_to_pre_retraction_schema,
    observation,
)

from baibai_engine.market_context.indicator_cli import (
    main,
)
from baibai_engine.market_context.indicator_service import (
    IndicatorsService,
)
from baibai_engine.market_context.indicators.db import (
    delete_unchanged_vintages,
    get_series,
    initialize_database,
    insert_observations,
    latest_observation,
    observations_in_range,
    retract_observations,
    row_count,
)
from baibai_engine.market_context.indicators.definitions import (
    IndicatorDefinitions,
    load_definitions,
)


class RetractionVintageTests(unittest.TestCase):
    """Taking a wrong observation out of the reads without taking it out of the store."""

    def test_retraction_hides_the_observation_date_from_range_and_latest_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = initialize_database(Path(tmp) / "indicators.sqlite")
            try:
                series = get_series(conn, "us.10y")
                insert_observations(
                    conn,
                    [
                        observation(
                            series, date(2026, 5, 1), 4.39, datetime(2026, 5, 2, tzinfo=UTC)
                        ),
                        observation(
                            series, date(2026, 5, 4), 4.41, datetime(2026, 5, 5, tzinfo=UTC)
                        ),
                    ],
                )
                retract_observations(
                    conn,
                    "us.10y",
                    [(date(2026, 5, 4), datetime(2026, 5, 5, tzinfo=UTC))],
                    vintage_at=datetime(2026, 5, 10, tzinfo=UTC),
                )
                conn.commit()

                observations = observations_in_range(
                    conn, "us.10y", date(2026, 5, 1), date(2026, 5, 31)
                )
                latest = latest_observation(conn, "us.10y")
                stored = row_count(conn, "observations")
            finally:
                conn.close()

            self.assertEqual([item.observed_at for item in observations], [date(2026, 5, 1)])
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest.observed_at, date(2026, 5, 1))
            self.assertEqual(stored, 3)

    def test_point_in_time_replay_before_the_retraction_still_reads_the_observation(self) -> None:
        """A replay must show what was believed then, not what is believed now."""

        with tempfile.TemporaryDirectory() as tmp:
            conn = initialize_database(Path(tmp) / "indicators.sqlite")
            try:
                series = get_series(conn, "jp.foreign_flows")
                insert_observations(
                    conn,
                    [
                        observation(
                            series,
                            date(2024, 8, 23),
                            -408854431.0,
                            datetime(2024, 8, 29, tzinfo=UTC),
                            period_days=4,
                        )
                    ],
                )
                retract_observations(
                    conn,
                    "jp.foreign_flows",
                    [(date(2024, 8, 23), datetime(2024, 8, 29, tzinfo=UTC))],
                    vintage_at=datetime(2024, 9, 15, tzinfo=UTC),
                )
                conn.commit()

                before = observations_in_range(
                    conn,
                    "jp.foreign_flows",
                    date(2024, 8, 1),
                    date(2024, 9, 30),
                    point_in_time=True,
                    vintage_on_or_before=date(2024, 9, 1),
                )
                after = observations_in_range(
                    conn,
                    "jp.foreign_flows",
                    date(2024, 8, 1),
                    date(2024, 9, 30),
                    point_in_time=True,
                    vintage_on_or_before=date(2024, 9, 30),
                )
            finally:
                conn.close()

            self.assertEqual([item.value for item in before], [-408854431.0])
            self.assertEqual(after, ())

    def test_a_newer_provider_vintage_revives_a_retracted_observation(self) -> None:
        """A source that asserts the date again outranks the retraction, which is correct."""

        with tempfile.TemporaryDirectory() as tmp:
            conn = initialize_database(Path(tmp) / "indicators.sqlite")
            try:
                series = get_series(conn, "us.10y")
                insert_observations(
                    conn,
                    [observation(series, date(2026, 5, 1), 4.39, datetime(2026, 5, 2, tzinfo=UTC))],
                )
                retract_observations(
                    conn,
                    "us.10y",
                    [(date(2026, 5, 1), datetime(2026, 5, 2, tzinfo=UTC))],
                    vintage_at=datetime(2026, 5, 10, tzinfo=UTC),
                )
                insert_observations(
                    conn,
                    [
                        observation(
                            series, date(2026, 5, 1), 4.44, datetime(2026, 5, 20, tzinfo=UTC)
                        )
                    ],
                )
                conn.commit()

                observations = observations_in_range(
                    conn, "us.10y", date(2026, 5, 1), date(2026, 5, 1)
                )
            finally:
                conn.close()

            self.assertEqual([item.value for item in observations], [4.44])

    def test_delete_unchanged_vintages_keeps_a_retraction_of_an_identical_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = initialize_database(Path(tmp) / "indicators.sqlite")
            try:
                series = get_series(conn, "us.10y")
                insert_observations(
                    conn,
                    [observation(series, date(2026, 5, 1), 4.39, datetime(2026, 5, 2, tzinfo=UTC))],
                )
                retract_observations(
                    conn,
                    "us.10y",
                    [(date(2026, 5, 1), datetime(2026, 5, 2, tzinfo=UTC))],
                    vintage_at=datetime(2026, 5, 10, tzinfo=UTC),
                )
                conn.commit()

                deleted = delete_unchanged_vintages(conn, "us.10y")
                observations = observations_in_range(
                    conn, "us.10y", date(2026, 5, 1), date(2026, 5, 1)
                )
                stored = row_count(conn, "observations")
            finally:
                conn.close()

            self.assertEqual(deleted, 0)
            self.assertEqual(observations, ())
            self.assertEqual(stored, 2)


    def test_retraction_falls_back_to_the_vintage_underneath_it(self) -> None:
        """A faulty writer stamping a wrong value over a good one must not cost the good one."""

        with tempfile.TemporaryDirectory() as tmp:
            conn = initialize_database(Path(tmp) / "indicators.sqlite")
            try:
                series = get_series(conn, "us.10y")
                insert_observations(
                    conn,
                    [
                        observation(
                            series, date(2026, 5, 1), 4.39, datetime(2026, 5, 2, tzinfo=UTC)
                        ),
                        observation(
                            series, date(2026, 5, 1), 9.99, datetime(2026, 6, 23, tzinfo=UTC)
                        ),
                    ],
                )
                outcomes = retract_observations(
                    conn,
                    "us.10y",
                    [(date(2026, 5, 1), datetime(2026, 6, 23, tzinfo=UTC))],
                    vintage_at=datetime(2026, 7, 27, tzinfo=UTC),
                )
                conn.commit()
                observations = observations_in_range(
                    conn, "us.10y", date(2026, 5, 1), date(2026, 5, 1)
                )
                stored = row_count(conn, "observations")
            finally:
                conn.close()

            self.assertEqual(outcomes[0].withdrawn.value, 9.99)
            self.assertIsNotNone(outcomes[0].restored)
            assert outcomes[0].restored is not None
            self.assertEqual(outcomes[0].restored.value, 4.39)
            self.assertEqual([item.value for item in observations], [4.39])
            self.assertEqual(stored, 3)

    def test_a_restored_observation_survives_a_merge_of_the_store_it_came_from(self) -> None:
        """The wrong vintage still exists on the other side; the fallback has to outrank it."""

        with tempfile.TemporaryDirectory() as tmp:
            conn = initialize_database(Path(tmp) / "indicators.sqlite")
            try:
                series = get_series(conn, "us.10y")
                wrong = observation(
                    series, date(2026, 5, 1), 9.99, datetime(2026, 6, 23, tzinfo=UTC)
                )
                insert_observations(
                    conn,
                    [
                        observation(
                            series, date(2026, 5, 1), 4.39, datetime(2026, 5, 2, tzinfo=UTC)
                        ),
                        wrong,
                    ],
                )
                retract_observations(
                    conn,
                    "us.10y",
                    [(date(2026, 5, 1), datetime(2026, 6, 23, tzinfo=UTC))],
                    vintage_at=datetime(2026, 7, 27, tzinfo=UTC),
                )
                # A merge restores the row the other store still holds.
                insert_observations(conn, [wrong], deduplicate_unchanged=False)
                conn.commit()
                observations = observations_in_range(
                    conn, "us.10y", date(2026, 5, 1), date(2026, 5, 1)
                )
            finally:
                conn.close()

            self.assertEqual([item.value for item in observations], [4.39])

    def test_repeating_a_retraction_refuses_instead_of_walking_back_down(self) -> None:
        """Without this, a second pass reads the restored row and puts the wrong value back."""

        with tempfile.TemporaryDirectory() as tmp:
            conn = initialize_database(Path(tmp) / "indicators.sqlite")
            try:
                series = get_series(conn, "us.10y")
                insert_observations(
                    conn,
                    [
                        observation(
                            series, date(2026, 5, 1), 4.39, datetime(2026, 5, 2, tzinfo=UTC)
                        ),
                        observation(
                            series, date(2026, 5, 1), 9.99, datetime(2026, 6, 23, tzinfo=UTC)
                        ),
                    ],
                )
                retract_observations(
                    conn,
                    "us.10y",
                    [(date(2026, 5, 1), datetime(2026, 6, 23, tzinfo=UTC))],
                    vintage_at=datetime(2026, 7, 27, tzinfo=UTC),
                )
                conn.commit()

                with self.assertRaisesRegex(ValueError, "not the latest vintage"):
                    retract_observations(
                        conn,
                        "us.10y",
                        [(date(2026, 5, 1), datetime(2026, 6, 23, tzinfo=UTC))],
                        vintage_at=datetime(2026, 7, 28, tzinfo=UTC),
                    )
                observations = observations_in_range(
                    conn, "us.10y", date(2026, 5, 1), date(2026, 5, 1)
                )
            finally:
                conn.close()

            self.assertEqual([item.value for item in observations], [4.39])

    def test_retracting_an_input_also_removes_its_derived_reading(self) -> None:
        """The old materialized value cannot outlive its withdrawn source input."""

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "indicators.sqlite"
            registry = load_definitions().by_id()
            definitions = IndicatorDefinitions(
                series=tuple(
                    registry[item] for item in ("us.10y", "jp.10y", "rate_diff.us_jp_10y")
                ),
                generation=1,
            )
            conn = initialize_database(database, definitions=definitions)
            try:
                vintage = datetime(2026, 7, 21, tzinfo=UTC)
                insert_observations(
                    conn,
                    [
                        observation(get_series(conn, "us.10y"), date(2026, 7, 20), 4.5, vintage),
                        observation(get_series(conn, "jp.10y"), date(2026, 7, 20), 1.6, vintage),
                        observation(
                            get_series(conn, "rate_diff.us_jp_10y"),
                            date(2026, 7, 20),
                            2.9,
                            vintage,
                        ),
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            service = IndicatorsService(database)

            service.retract("us.10y", [(date(2026, 7, 20), vintage)])
            result = service.get_range(
                "rate_diff.us_jp_10y", start=date(2026, 7, 20), end=date(2026, 7, 20)
            )
            self.assertEqual(result.observations, ())

            conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                kept = observations_in_range(conn, "us.10y", date(2026, 7, 20), date(2026, 7, 20))
            finally:
                conn.close()

            self.assertEqual(kept, ())


    def test_retract_refuses_a_date_the_store_never_observed(self) -> None:
        """A retraction list that no longer matches the store is a stale list, not a no-op."""

        with tempfile.TemporaryDirectory() as tmp:
            conn = initialize_database(Path(tmp) / "indicators.sqlite")
            try:
                with self.assertRaisesRegex(ValueError, "no observation to retract"):
                    retract_observations(
                        conn,
                        "us.10y",
                        [(date(2026, 5, 1), datetime(2026, 5, 2, tzinfo=UTC))],
                        vintage_at=datetime(2026, 5, 10, tzinfo=UTC),
                    )
            finally:
                conn.close()

    def test_retracting_an_already_retracted_date_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = initialize_database(Path(tmp) / "indicators.sqlite")
            try:
                series = get_series(conn, "us.10y")
                insert_observations(
                    conn,
                    [observation(series, date(2026, 5, 1), 4.39, datetime(2026, 5, 2, tzinfo=UTC))],
                )
                retract_observations(
                    conn,
                    "us.10y",
                    [(date(2026, 5, 1), datetime(2026, 5, 2, tzinfo=UTC))],
                    vintage_at=datetime(2026, 5, 10, tzinfo=UTC),
                )
                again = retract_observations(
                    conn,
                    "us.10y",
                    [(date(2026, 5, 1), datetime(2026, 5, 10, tzinfo=UTC))],
                    vintage_at=datetime(2026, 5, 11, tzinfo=UTC),
                )
                conn.commit()
                stored = row_count(conn, "observations")
            finally:
                conn.close()

            self.assertEqual(again, ())
            self.assertEqual(stored, 2)

    def test_retract_cli_names_every_observation_it_withdrew(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "indicators.sqlite"
            conn = initialize_database(database)
            try:
                series = get_series(conn, "us.10y")
                insert_observations(
                    conn,
                    [observation(series, date(2026, 5, 1), 4.39, datetime(2026, 5, 2, tzinfo=UTC))],
                )
                conn.commit()
            finally:
                conn.close()
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "retract",
                        "us.10y",
                        "--observed-at",
                        "2026-05-01",
                        "--expected-vintage",
                        "2026-05-02T00:00:00+00:00",
                        "--db",
                        str(database),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("us.10y\t2026-05-01\twithdrawn\t4.39\t-", stdout.getvalue())
            self.assertIn("0 fell back to an earlier vintage, 1 left the reads", stdout.getvalue())

    def test_the_previous_fetch_status_domain_cannot_hold_a_retraction(self) -> None:
        """The widened CHECK is what makes a retraction storable at all, so pin it."""

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "indicators.sqlite"
            conn = initialize_database(database)
            try:
                series = get_series(conn, "us.10y")
                insert_observations(
                    conn,
                    [observation(series, date(2026, 5, 1), 4.39, datetime(2026, 5, 2, tzinfo=UTC))],
                )
                conn.commit()
            finally:
                conn.close()
            downgrade_to_pre_retraction_schema(database)

            legacy = sqlite3.connect(database)
            legacy.row_factory = sqlite3.Row
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    retract_observations(
                        legacy,
                        "us.10y",
                        [(date(2026, 5, 1), datetime(2026, 5, 2, tzinfo=UTC))],
                        vintage_at=datetime(2026, 5, 10, tzinfo=UTC),
                    )
            finally:
                legacy.close()
