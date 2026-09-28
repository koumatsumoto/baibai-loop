from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.engine.macro.indicator_fixtures import (
    _prune_store_rows,
    _retired_counts,
    _series,
    _write_retired_series,
)
from tests.helpers.indicator_store import (
    observation,
)

import baibai_engine.macro.indicators.db as indicators_db
from baibai_engine.macro.indicators.cli import (
    main,
)
from baibai_engine.macro.indicators.db import (
    SQLITE_SCHEMA_VERSION,
    IndicatorsSchemaError,
    ObservationRecord,
    delete_unchanged_vintages,
    get_series,
    has_ok_coverage,
    initialize_database,
    insert_observations,
    list_series,
    observations_in_range,
    open_connection,
    record_provider_run,
    row_count,
)
from baibai_engine.macro.indicators.definitions import (
    IndicatorDefinitions,
    load_definitions,
)
from baibai_engine.macro.indicators.service import (
    IndicatorsService,
    _prune_registry_for_refresh,
)
from baibai_engine.macro.indicators.service import list_series as catalog_list_series
from baibai_engine.macro.indicators.service import search as catalog_search
from baibai_engine.macro.reading.cli import main as reading_main


class IndicatorsDBTests(unittest.TestCase):
    def test_read_only_connection_encodes_uri_metacharacters_and_rejects_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            real_db = directory / "real.sqlite"
            initialize_database(real_db).close()
            crafted = directory / "real.sqlite?mode=rw&x="
            crafted.touch()

            connection = indicators_db.open_read_only_connection(crafted)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("CREATE TABLE write_probe(value INTEGER)")
            finally:
                connection.close()

            with sqlite3.connect(real_db) as check:
                self.assertIsNone(
                    check.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'write_probe'"
                    ).fetchone()
                )

    def test_initialize_database_seeds_series_and_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            conn = initialize_database(db)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                series = get_series(conn, "us.10y")
                alias_count = row_count(conn, "aliases")
            finally:
                conn.close()

            self.assertEqual(version, SQLITE_SCHEMA_VERSION)
            self.assertEqual(series.name, "米10Y利回り")
            self.assertEqual((series.plausible_min, series.plausible_max), (-20.0, 30.0))
            self.assertGreater(alias_count, 0)

    def test_the_immediately_previous_schema_is_migrated(self) -> None:

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            initialize_database(database).close()
            with sqlite3.connect(database) as connection:
                connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")

            connection = open_connection(database)
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(version, SQLITE_SCHEMA_VERSION)

    def test_a_failed_previous_schema_migration_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            initialize_database(database).close()
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE registry_prune_authorizations(
                      series_id TEXT PRIMARY KEY REFERENCES series(series_id) ON DELETE CASCADE
                    );
                    CREATE TRIGGER protect_series_from_implicit_prune
                    BEFORE DELETE ON series
                    WHEN NOT EXISTS(
                      SELECT 1 FROM registry_prune_authorizations
                      WHERE series_id = OLD.series_id
                    )
                    BEGIN
                      SELECT RAISE(ABORT, 'explicit registry prune authorization required');
                    END;
                    CREATE TRIGGER unexpected_migration_trigger
                    BEFORE INSERT ON aliases
                    BEGIN
                      SELECT RAISE(ABORT, 'unexpected');
                    END;
                    """
                )
                connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")

            with self.assertRaisesRegex(IndicatorsSchemaError, "trigger contract mismatch"):
                open_connection(database)

            with sqlite3.connect(database) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                retired_objects = connection.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name IN "
                    "('registry_prune_authorizations', "
                    "'protect_series_from_implicit_prune')"
                ).fetchone()[0]
            self.assertEqual(version, SQLITE_SCHEMA_VERSION - 1)
            self.assertEqual(retired_objects, 2)

    def test_an_unsupported_older_schema_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            initialize_database(database).close()
            with sqlite3.connect(database) as connection:
                connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 2}")

            with self.assertRaisesRegex(
                IndicatorsSchemaError,
                f"unsupported indicator SQLite schema: {SQLITE_SCHEMA_VERSION - 2}",
            ):
                open_connection(database)

    def test_open_connection_preserves_series_removed_from_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            _write_retired_series(db)
            conn = open_connection(db)
            try:
                series_ids = {series.series_id for series in list_series(conn)}
                stale_rows = conn.execute(
                    "SELECT COUNT(*) FROM observations WHERE series_id = ?",
                    ("jp.cpi.stale",),
                ).fetchone()[0]
                stale_runs = conn.execute(
                    "SELECT COUNT(*) FROM provider_runs WHERE series_id = ?",
                    ("jp.cpi.stale",),
                ).fetchone()[0]
                stale_aliases = conn.execute(
                    "SELECT COUNT(*) FROM aliases WHERE series_id = ?",
                    ("jp.cpi.stale",),
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertIn("jp.cpi.stale", series_ids)
            self.assertEqual(stale_rows, 1)
            self.assertEqual(stale_runs, 1)
            self.assertEqual(stale_aliases, 1)

    def test_read_paths_hide_retired_series_without_deleting_its_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            _write_retired_series(database)

            service = IndicatorsService(database)
            with self.assertRaisesRegex(KeyError, "unknown indicator series"):
                service.get_range(
                    "jp.cpi.stale",
                    start=date(2021, 12, 1),
                    end=date(2021, 12, 1),
                )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                reading_exit = reading_main(["--asof", "2021-12-01", "--db", str(database)])

            conn = sqlite3.connect(database)
            try:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM observations WHERE series_id = ?",
                    ("jp.cpi.stale",),
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertNotIn("jp.cpi.stale", {item.series_id for item in catalog_list_series()})
            self.assertEqual(catalog_search("stale"), ())
            self.assertEqual(reading_exit, 0)
            self.assertNotIn("jp.cpi.stale", stdout.getvalue())
            self.assertEqual(remaining, 1)

    def test_reading_cli_replays_point_in_time_provider_vintage_at_asof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            conn = initialize_database(database)
            try:
                insert_observations(
                    conn,
                    [
                        ObservationRecord(
                            series_id="jp.foreign_flows",
                            observed_at=date(2024, 8, 23),
                            value=value,
                            unit="jpy-thousand",
                            source_url="https://jpx-jquants.com/ja/spec/eq-investor-types",
                            period_start=date(2024, 8, 19),
                            period_end=date(2024, 8, 23),
                            vintage_at=vintage_at,
                        )
                        for value, vintage_at in (
                            (-408854431.0, datetime(2024, 8, 29, tzinfo=UTC)),
                            (-400000000.0, datetime(2024, 9, 10, tzinfo=UTC)),
                        )
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = reading_main(
                    [
                        "--asof",
                        "2024-08-31",
                        "--db",
                        str(database),
                        "--format",
                        "json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            readings = {item["series_id"]: item for item in payload["series"]}
            self.assertEqual(
                readings["jp.foreign_flows"]["latest_value"],
                -408854431.0,
            )
            self.assertEqual(
                readings["jp.foreign_flows"]["next_print_estimate"],
                "2024-09-06",
            )
            self.assertEqual(
                readings["jp.foreign_flows"]["print_due_in_days"],
                6,
            )

    def test_refresh_prunes_retired_series_and_reports_deleted_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            _write_retired_series(database)
            definition = load_definitions().by_id()["us.10y"]
            refreshed = ObservationRecord(
                series_id=definition.series_id,
                observed_at=date(2026, 5, 1),
                value=4.39,
                unit=definition.unit,
                source_url=definition.source_url,
                vintage_at=datetime(2026, 5, 2, tzinfo=UTC),
            )
            stdout = io.StringIO()

            with (
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[refreshed],
                ),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "refresh",
                        "us.10y",
                        "--start",
                        "2026-05-01",
                        "--end",
                        "2026-05-01",
                        "--db",
                        str(database),
                    ]
                )

            conn = sqlite3.connect(database)
            try:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM series WHERE series_id = ?",
                    ("jp.cpi.stale",),
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(exit_code, 0)
            self.assertIn(
                "registry-prune\tjp.cpi.stale\tobservations=1\tprovider_runs=1",
                stdout.getvalue(),
            )
            self.assertNotIn("registry-prune-pending", stdout.getvalue())
            self.assertNotIn("transaction=", stdout.getvalue())
            self.assertEqual(remaining, 0)

    def test_refresh_refuses_to_prune_a_membership_written_at_the_same_generation(self) -> None:
        """Another working tree's facts are not this client's to delete.

        One generation is one membership, so a store that carries a series this
        registry does not name while claiming the same generation was written by a
        different registry snapshot — a parallel branch sharing the store path, not
        a retirement this client authorised.
        """

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            _write_retired_series(database)
            with sqlite3.connect(database) as connection:
                indicators_db.set_registry_generation(connection, load_definitions().generation)

            with self.assertRaisesRegex(ValueError, "does not name at the same generation"):
                IndicatorsService(database).refresh_series(
                    ["us.10y"],
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                )

            self.assertEqual(_retired_counts(database), (1, 1, 1))

    def test_refresh_prune_failure_rolls_back_every_retired_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            _write_retired_series(database)
            open_connection(database).close()
            before = _prune_store_rows(database)
            conn = sqlite3.connect(database)
            try:
                conn.execute(
                    "CREATE TRIGGER block_retired_series_delete "
                    "BEFORE DELETE ON series "
                    "WHEN OLD.series_id = 'jp.cpi.stale' "
                    "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
                )
                conn.commit()
            finally:
                conn.close()

            with (
                # This test deliberately injects a non-canonical trigger to
                # exercise prune rollback after open; schema-drift rejection is
                # covered independently below.
                patch("baibai_engine.macro.indicators.db._validate_trigger_contract"),
                self.assertRaisesRegex(sqlite3.IntegrityError, "blocked"),
            ):
                IndicatorsService(database).refresh_series(
                    ["us.10y"],
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                )

            self.assertEqual(_retired_counts(database), (1, 1, 1))
            self.assertEqual(_prune_store_rows(database), before)

    def test_newer_store_registry_generation_rejects_stale_client_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            definitions = load_definitions()
            initialize_database(
                database,
                definitions=IndicatorDefinitions(
                    series=definitions.series,
                    generation=definitions.generation + 1,
                ),
            ).close()

            newer = definitions.generation + 1
            with self.assertRaisesRegex(
                ValueError,
                rf"store={newer}, client={definitions.generation}.*refusing refresh",
            ):
                IndicatorsService(database).refresh_series(
                    ["us.10y"],
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                )

            with sqlite3.connect(database) as connection:
                generation = connection.execute(
                    "SELECT generation FROM registry_state WHERE singleton = 1"
                ).fetchone()[0]
            self.assertEqual(generation, newer)

    def test_refresh_prune_reporting_failure_keeps_committed_rows(self) -> None:
        for error_type in (BrokenPipeError, OSError):
            with self.subTest(error=error_type), tempfile.TemporaryDirectory() as tmp:
                database = Path(tmp) / "macro.sqlite"
                _write_retired_series(database)
                committed: list[bool] = []

                def closed_output(
                    value: object,
                    *,
                    database: Path = database,
                    committed: list[bool] = committed,
                    error_type: type[OSError] = error_type,
                    **kwargs: object,
                ) -> None:
                    if not str(value).startswith("registry-prune\t"):
                        return
                    self.assertTrue(kwargs["flush"])
                    self.assertEqual(_retired_counts(database), (0, 0, 0))
                    with sqlite3.connect(database) as check:
                        generation = indicators_db.registry_generation(check)
                    self.assertEqual(generation, load_definitions().generation)
                    committed.append(True)
                    raise error_type("closed")

                with (
                    patch(
                        "baibai_engine.macro.indicators.service.print",
                        side_effect=closed_output,
                        create=True,
                    ),
                    patch(
                        "baibai_engine.macro.indicators.service.fetch_observations",
                        return_value=[observation("us.10y", date(2026, 5, 1), 4.39)],
                    ),
                ):
                    IndicatorsService(database).refresh_series(
                        ["us.10y"], start=date(2026, 5, 1), end=date(2026, 5, 1)
                    )
                self.assertEqual(committed, [True])
                self.assertEqual(_retired_counts(database), (0, 0, 0))

    def test_refresh_prune_counts_and_deletes_under_one_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            _write_retired_series(database)
            original_prune = indicators_db.prune_definitions
            blocked: list[bool] = []

            def probe() -> None:
                connection = sqlite3.connect(database, timeout=0)
                try:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        connection.execute(
                            "UPDATE registry_state SET generation = generation WHERE singleton = 1"
                        )
                    blocked.append(True)
                finally:
                    connection.close()

            def checked_prune(connection, definitions):
                probe()
                result = original_prune(connection, definitions)
                probe()
                return result

            with (
                patch(
                    "baibai_engine.macro.indicators.service.db.prune_definitions",
                    side_effect=checked_prune,
                ),
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[observation("us.10y", date(2026, 5, 1), 4.39)],
                ),
            ):
                IndicatorsService(database).refresh_series(
                    ["us.10y"], start=date(2026, 5, 1), end=date(2026, 5, 1)
                )
            self.assertEqual(blocked, [True, True])
            self.assertEqual(_retired_counts(database), (0, 0, 0))
            with sqlite3.connect(database, timeout=0) as check:
                check.execute(
                    "UPDATE registry_state SET generation = generation WHERE singleton = 1"
                )

    def test_refresh_prune_commit_failure_rolls_back_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            _write_retired_series(database)
            connection = open_connection(database)
            before = _prune_store_rows(database)
            failing_connection = MagicMock(wraps=connection)
            failing_connection.commit.side_effect = sqlite3.OperationalError("commit failed")
            try:
                with (
                    patch("baibai_engine.macro.indicators.service.print", create=True) as report,
                    self.assertRaisesRegex(sqlite3.OperationalError, "commit failed"),
                ):
                    _prune_registry_for_refresh(failing_connection, load_definitions())
                report.assert_not_called()
            finally:
                connection.close()
            self.assertEqual(_prune_store_rows(database), before)

    def test_refresh_without_retired_rows_updates_generation_without_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            definitions = load_definitions()
            connection = initialize_database(
                database,
                definitions=IndicatorDefinitions(
                    series=definitions.series,
                    generation=definitions.generation - 1,
                ),
            )
            try:
                with patch("baibai_engine.macro.indicators.service.print", create=True) as report:
                    _prune_registry_for_refresh(connection, definitions)
                report.assert_not_called()
            finally:
                connection.close()
            with sqlite3.connect(database) as check:
                self.assertEqual(indicators_db.registry_generation(check), definitions.generation)

    def test_observations_in_range_returns_latest_vintage_per_observed_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            conn = initialize_database(db)
            try:
                series = get_series(conn, "us.10y")
                insert_observations(
                    conn,
                    [
                        ObservationRecord(
                            series_id="us.10y",
                            observed_at=date(2026, 5, 1),
                            value=4.39,
                            unit=series.unit,
                            source_url=series.source_url,
                            vintage_at=datetime(2026, 5, 1, tzinfo=UTC),
                        ),
                        ObservationRecord(
                            series_id="us.10y",
                            observed_at=date(2026, 5, 1),
                            value=4.41,
                            unit=series.unit,
                            source_url=series.source_url,
                            vintage_at=datetime(2026, 5, 2, tzinfo=UTC),
                        ),
                    ],
                )
                conn.commit()

                observations = observations_in_range(
                    conn,
                    "us.10y",
                    date(2026, 5, 1),
                    date(2026, 5, 1),
                )
            finally:
                conn.close()

            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].value, 4.41)

    def test_insert_gate_rejects_out_of_range_value_and_wrong_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            conn = initialize_database(database)
            try:
                for record, expected in (
                    (observation("us.10y", date(2026, 5, 1), 999.0), "plausible range"),
                    (
                        observation("us.10y", date(2026, 5, 1), 4.39, unit="basis-points"),
                        "series unit",
                    ),
                ):
                    with (
                        self.subTest(expected=expected),
                        self.assertRaisesRegex(sqlite3.IntegrityError, expected),
                    ):
                        insert_observations(conn, [record])
                    conn.rollback()
                stored = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(stored, 0)

    def test_insert_gate_rolls_back_the_entire_batch_after_a_late_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            conn = initialize_database(database)
            try:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "plausible range"):
                    insert_observations(
                        conn,
                        [
                            observation("us.10y", date(2026, 5, 1), 4.39),
                            observation("us.10y", date(2026, 5, 2), 999.0),
                        ],
                    )
                conn.commit()
                stored = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(stored, 0)

    def test_persistent_gate_rejects_unknown_series_when_foreign_keys_are_disabled(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            conn = initialize_database(database)
            try:
                insert_observations(conn, [observation("us.10y", date(2026, 5, 1), 4.39)])
                conn.commit()
            finally:
                conn.close()

            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 0)
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "observation violates series unit or plausible range",
                ):
                    connection.execute(
                        "INSERT INTO observations("
                        "series_id, observed_at, value, unit, vintage_at, fetch_status, source_url"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            "unknown.series",
                            "2026-05-02",
                            4.0,
                            "percent",
                            "2026-05-03T00:00:00+00:00",
                            "ok",
                            "https://example.com/data.csv",
                        ),
                    )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "observation violates series unit or plausible range",
                ):
                    connection.execute(
                        "UPDATE observations SET series_id = 'unknown.series' "
                        "WHERE series_id = 'us.10y'"
                    )
                connection.commit()
                rows = list(
                    connection.execute(
                        "SELECT series_id, observed_at FROM observations ORDER BY observed_at"
                    )
                )

            self.assertEqual(rows, [("us.10y", "2026-05-01")])

    def test_insert_gate_preserves_the_callers_prior_transaction_on_batch_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            conn = initialize_database(database)
            try:
                insert_observations(
                    conn,
                    [observation("us.10y", date(2026, 4, 30), 4.30)],
                )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "plausible range"):
                    insert_observations(
                        conn,
                        [
                            observation("us.10y", date(2026, 5, 1), 4.39),
                            observation("us.10y", date(2026, 5, 2), 999.0),
                        ],
                    )
                conn.commit()
                stored = [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT observed_at, value FROM observations ORDER BY observed_at"
                    )
                ]
            finally:
                conn.close()

            self.assertEqual(stored, [("2026-04-30", 4.3)])

    def test_insert_gate_rolls_back_prior_rows_when_a_late_conflict_update_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            conn = initialize_database(database)
            existing = observation("us.10y", date(2026, 5, 2), 4.40)
            try:
                insert_observations(conn, [existing])
                conn.commit()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "plausible range"):
                    insert_observations(
                        conn,
                        [
                            observation("us.10y", date(2026, 5, 1), 4.39),
                            ObservationRecord(
                                series_id="us.10y",
                                observed_at=existing.observed_at,
                                value=999.0,
                                unit=existing.unit,
                                source_url=existing.source_url,
                                vintage_at=existing.vintage_at,
                            ),
                        ],
                        deduplicate_unchanged=False,
                    )
                conn.commit()
                stored = [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT observed_at, value FROM observations ORDER BY observed_at"
                    )
                ]
            finally:
                conn.close()

            self.assertEqual(stored, [("2026-05-02", 4.4)])

    def test_update_gate_preserves_valid_observation_on_contract_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            conn = initialize_database(database)
            try:
                insert_observations(conn, [observation("us.10y", date(2026, 5, 1), 4.39)])
                conn.commit()

                with self.assertRaisesRegex(sqlite3.IntegrityError, "plausible range"):
                    conn.execute("UPDATE observations SET value = 999 WHERE series_id = 'us.10y'")
                conn.rollback()
                stored = conn.execute(
                    "SELECT value FROM observations WHERE series_id = 'us.10y'"
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(stored, 4.39)

    def test_registry_update_rejects_a_band_that_excludes_existing_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            original = _series("fred_csv", "TEST")
            conn = initialize_database(
                database,
                definitions=IndicatorDefinitions(series=(original,)),
            )
            try:
                insert_observations(
                    conn,
                    [observation("test.series", date(2026, 5, 1), 100.0)],
                )
                conn.commit()
            finally:
                conn.close()
            narrowed = _series(
                "fred_csv",
                "TEST",
                plausible_min=-2.0,
                plausible_max=20.0,
            )

            with self.assertRaisesRegex(
                IndicatorsSchemaError,
                r"test\.series 2026-05-01.*value 100 outside plausible range \[-2, 20\]",
            ):
                open_connection(
                    database,
                    definitions=IndicatorDefinitions(series=(narrowed,)),
                )

            with sqlite3.connect(database) as connection:
                stored = connection.execute(
                    "SELECT plausible_min, plausible_max FROM series "
                    "WHERE series_id = 'test.series'"
                ).fetchone()
            self.assertEqual(stored, (None, None))

    def test_series_contract_update_trigger_rejects_excluded_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            definition = _series("fred_csv", "TEST")
            conn = initialize_database(
                database,
                definitions=IndicatorDefinitions(series=(definition,)),
            )
            try:
                insert_observations(
                    conn,
                    [observation("test.series", date(2026, 5, 1), 100.0)],
                )
                conn.commit()

                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "series contract excludes an existing observation",
                ):
                    conn.execute(
                        "UPDATE series SET plausible_max = 20 WHERE series_id = 'test.series'"
                    )
                conn.rollback()
                stored = conn.execute(
                    "SELECT plausible_max FROM series WHERE series_id = 'test.series'"
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertIsNone(stored)

    def test_current_schema_rejects_missing_changed_or_unexpected_contract_trigger(
        self,
    ) -> None:
        for mode in ("missing", "changed", "unexpected"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                database = Path(tmp) / "macro.sqlite"
                initialize_database(database).close()
                with sqlite3.connect(database) as connection:
                    if mode in {"missing", "changed"}:
                        connection.execute(
                            "DROP TRIGGER validate_observation_plausibility_before_insert"
                        )
                    if mode == "changed":
                        connection.execute(
                            "CREATE TRIGGER validate_observation_plausibility_before_insert "
                            "BEFORE INSERT ON observations BEGIN SELECT 1; END"
                        )
                    if mode == "unexpected":
                        connection.execute(
                            "CREATE TRIGGER unexpected_observation_trigger "
                            "AFTER INSERT ON observations BEGIN SELECT 1; END"
                        )

                with self.assertRaisesRegex(
                    IndicatorsSchemaError,
                    f"trigger contract mismatch.*{mode}",
                ):
                    open_connection(database)

    def test_jquants_range_excludes_vintages_published_after_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            conn = initialize_database(database)
            try:
                insert_observations(
                    conn,
                    [
                        ObservationRecord(
                            series_id="jp.foreign_flows",
                            observed_at=date(2024, 8, 23),
                            value=value,
                            unit="jpy-thousand",
                            source_url="https://jpx-jquants.com/ja/spec/eq-investor-types",
                            period_start=date(2024, 8, 19),
                            period_end=date(2024, 8, 23),
                            vintage_at=vintage_at,
                        )
                        for value, vintage_at in (
                            (-408854431.0, datetime(2024, 8, 29, tzinfo=UTC)),
                            (-400000000.0, datetime(2024, 9, 10, tzinfo=UTC)),
                        )
                    ],
                )

                august = observations_in_range(
                    conn,
                    "jp.foreign_flows",
                    date(2024, 8, 1),
                    date(2024, 8, 31),
                    point_in_time=True,
                )
                september = observations_in_range(
                    conn,
                    "jp.foreign_flows",
                    date(2024, 8, 1),
                    date(2024, 9, 30),
                    point_in_time=True,
                )
            finally:
                conn.close()

            self.assertEqual([item.value for item in august], [-408854431.0])
            self.assertEqual([item.value for item in september], [-400000000.0])

    def test_insert_observations_skips_unchanged_later_vintage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            conn = initialize_database(database)
            try:
                common = {
                    "series_id": "us.10y",
                    "observed_at": date(2026, 5, 1),
                    "unit": "percent",
                    "source_url": "https://example.com/us10y.csv",
                }
                insert_observations(
                    conn,
                    [
                        ObservationRecord(
                            **common,
                            value=4.39,
                            vintage_at=datetime(2026, 5, 2, tzinfo=UTC),
                        )
                    ],
                )
                insert_observations(
                    conn,
                    [
                        ObservationRecord(
                            **common,
                            value=4.39,
                            vintage_at=datetime(2026, 5, 3, tzinfo=UTC),
                        ),
                        ObservationRecord(
                            **common,
                            value=4.41,
                            vintage_at=datetime(2026, 5, 4, tzinfo=UTC),
                        ),
                        ObservationRecord(
                            **common,
                            value=4.39,
                            vintage_at=datetime(2026, 5, 5, tzinfo=UTC),
                        ),
                    ],
                )
                rows = conn.execute(
                    "SELECT value, vintage_at FROM observations WHERE series_id = ? "
                    "AND observed_at = ? ORDER BY vintage_at",
                    ("us.10y", "2026-05-01"),
                ).fetchall()
            finally:
                conn.close()

            self.assertEqual(
                [(row["value"], row["vintage_at"]) for row in rows],
                [
                    (4.39, "2026-05-02T00:00:00+00:00"),
                    (4.41, "2026-05-04T00:00:00+00:00"),
                    (4.39, "2026-05-05T00:00:00+00:00"),
                ],
            )

    def test_delete_unchanged_vintages_preserves_changed_and_reverted_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            conn = initialize_database(database)
            try:
                insert_observations(
                    conn,
                    [
                        ObservationRecord(
                            series_id="us.10y",
                            observed_at=date(2026, 5, 1),
                            value=value,
                            unit="percent",
                            source_url="https://example.com/us10y.csv",
                            vintage_at=datetime(2026, 5, day, tzinfo=UTC),
                        )
                        for day, value in ((1, 4.39), (2, 4.39), (3, 4.41), (4, 4.39))
                    ],
                    deduplicate_unchanged=False,
                )

                deleted = delete_unchanged_vintages(conn, "us.10y")
                values = [
                    row[0]
                    for row in conn.execute(
                        "SELECT value FROM observations WHERE series_id = ? ORDER BY vintage_at",
                        ("us.10y",),
                    )
                ]
            finally:
                conn.close()

            self.assertEqual(deleted, 1)
            self.assertEqual(values, [4.39, 4.41, 4.39])

    def test_zero_record_provider_run_is_not_cache_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            conn = initialize_database(db)
            try:
                record_provider_run(
                    conn,
                    provider="fred_csv",
                    series_id="us.cpi.headline",
                    start=date(2027, 1, 1),
                    end=date(2027, 3, 31),
                    started_at=datetime(2026, 5, 1, tzinfo=UTC),
                    status="ok",
                    record_count=0,
                )
                conn.commit()

                covered = has_ok_coverage(
                    conn,
                    "us.cpi.headline",
                    date(2027, 1, 1),
                    date(2027, 3, 31),
                )
            finally:
                conn.close()

            self.assertFalse(covered)

    def test_aliases_allow_same_alias_for_multiple_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            conn = initialize_database(db)
            try:
                conn.execute(
                    "INSERT INTO aliases(alias, series_id) VALUES (?, ?)",
                    ("CPI", "us.cpi.headline"),
                )
                conn.execute(
                    "INSERT INTO aliases(alias, series_id) VALUES (?, ?)",
                    ("CPI", "us.cpi.core"),
                )
                conn.commit()
                rows = conn.execute(
                    "SELECT series_id FROM aliases WHERE alias = ? ORDER BY series_id",
                    ("CPI",),
                ).fetchall()
            finally:
                conn.close()

            self.assertEqual([row[0] for row in rows], ["us.cpi.core", "us.cpi.headline"])
