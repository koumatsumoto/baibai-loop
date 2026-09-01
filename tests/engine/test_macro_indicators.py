from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

import openpyxl
import requests
from tests.helpers.indicator_store import (
    downgrade_to_previous_schema,
    observation,
    series_definition,
)

import baibai_engine.macro.indicators.db as indicators_db
from baibai_engine.macro.indicators.cli import (
    build_parser,
    main,
    parse_refresh_failure_count,
)
from baibai_engine.macro.indicators.db import (
    RETRACTED_STATUS,
    SQLITE_SCHEMA_VERSION,
    IndicatorsSchemaError,
    ObservationRecord,
    delete_unchanged_vintages,
    get_series,
    has_ok_coverage,
    initialize_database,
    insert_observations,
    latest_observation,
    list_series,
    observations_in_range,
    open_connection,
    record_provider_run,
    retract_observations,
    row_count,
)
from baibai_engine.macro.indicators.definitions import (
    IndicatorDefinitions,
    SeriesDefinition,
    load_definitions,
)
from baibai_engine.macro.indicators.providers import (
    IndicatorsProviderError,
    fetch_observations,
    parse_boj_timeseries_json,
    parse_boj_xlsx,
    parse_ecb_fx_csv,
    parse_estat_json,
    parse_fred_csv,
    parse_h15_csv,
    parse_mof_jgb_csv,
    parse_multpl_current,
    parse_multpl_history,
    parse_trades_spec,
    parse_tsr_bankruptcies_json,
    parse_yahoo_chart,
    point_in_time_providers,
    registered_specs,
    spglobal_pmi,
)
from baibai_engine.macro.indicators.providers.base import (
    MAX_CSV_RESPONSE_BYTES,
    BrowserUnavailableError,
    FetchContext,
    HttpSession,
    SourceWithheldError,
    fetch_bytes,
    fetch_text,
    store_fetched_bytes,
)
from baibai_engine.macro.indicators.providers.cftc import parse_cftc_json
from baibai_engine.macro.indicators.providers.derived import DerivedProvider
from baibai_engine.macro.indicators.providers.estat_dashboard import (
    EStatDashboardProvider,
    parse_dashboard_json,
)
from baibai_engine.macro.indicators.providers.estat_dashboard import (
    source_url_selectors as estat_dashboard_selectors,
)
from baibai_engine.macro.indicators.providers.formulas import FORMULAS, DerivedComputationError
from baibai_engine.macro.indicators.providers.frb_h15 import FrbH15Provider
from baibai_engine.macro.indicators.providers.jquants_indices import parse_index_bars
from baibai_engine.macro.indicators.providers.nikkei_indexes import parse_nikkei_valuation
from baibai_engine.macro.indicators.providers.pmi_extraction import (
    PmiExtractionError,
    extract_pmi_value,
)
from baibai_engine.macro.indicators.providers.spglobal_pmi import (
    MANIFEST_PATH,
    MAX_PMI_PDF_BYTES,
    Release,
    SpGlobalPmiProvider,
    _parse_stream,
    extract_pdf_text,
    load_manifest,
    release_text,
)
from baibai_engine.macro.indicators.providers.umich_sca import parse_umich_table
from baibai_engine.macro.indicators.service import (
    IndicatorsService,
    RefreshFailure,
    RefreshSuccess,
)
from baibai_engine.macro.reading.cli import main as reading_main

if TYPE_CHECKING:
    # Only the annotation is needed; importing it for real would pull Playwright
    # onto the import path of the whole suite, which the lazy launch avoids.
    from baibai_engine.macro.indicators.providers.browser import BrowserFetcher


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

            self.assertNotIn("jp.cpi.stale", {item.series_id for item in service.list_series()})
            self.assertEqual(service.search("stale"), ())
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
            self.assertIn("registry-prune-pending\tjp.cpi.stale", stdout.getvalue())
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

    def test_refresh_prune_log_failure_rolls_back_every_retired_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            _write_retired_series(database)

            with (
                patch("builtins.print", side_effect=BrokenPipeError("closed")),
                self.assertRaisesRegex(BrokenPipeError, "closed"),
            ):
                IndicatorsService(database).refresh_series(
                    ["us.10y"],
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                )

            self.assertEqual(_retired_counts(database), (1, 1, 1))

    def test_refresh_post_commit_log_failure_leaves_pending_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            _write_retired_series(database)
            emitted: list[str] = []

            def _audit_print(value: object, **_: object) -> None:
                line = str(value)
                emitted.append(line)
                if line.startswith("registry-prune\t"):
                    raise BrokenPipeError("closed after commit")

            with (
                patch("builtins.print", side_effect=_audit_print),
                self.assertRaisesRegex(BrokenPipeError, "closed after commit"),
            ):
                IndicatorsService(database).refresh_series(
                    ["us.10y"],
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                )

            self.assertEqual(_retired_counts(database), (0, 0, 0))
            self.assertTrue(emitted[0].startswith("registry-prune-pending\t"))
            self.assertTrue(emitted[1].startswith("registry-prune\t"))

    def test_refresh_prune_counts_and_deletes_under_one_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            _write_retired_series(database)
            blocked: list[bool] = []

            def _try_concurrent_write(value: object, **_: object) -> None:
                if not str(value).startswith("registry-prune-pending\t"):
                    return
                connection = sqlite3.connect(database, timeout=0)
                try:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        connection.execute(
                            "UPDATE observations SET value = 101 WHERE series_id = 'jp.cpi.stale'"
                        )
                    blocked.append(True)
                finally:
                    connection.close()

            with (
                patch("builtins.print", side_effect=_try_concurrent_write),
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[observation("us.10y", date(2026, 5, 1), 4.39)],
                ),
            ):
                IndicatorsService(database).refresh_series(
                    ["us.10y"],
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                )

            self.assertEqual(blocked, [True])
            self.assertEqual(_retired_counts(database), (0, 0, 0))

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


class RetractionVintageTests(unittest.TestCase):
    """Taking a wrong observation out of the reads without taking it out of the store."""

    def test_retraction_hides_the_observation_date_from_range_and_latest_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = initialize_database(Path(tmp) / "macro.sqlite")
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
            conn = initialize_database(Path(tmp) / "macro.sqlite")
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
            conn = initialize_database(Path(tmp) / "macro.sqlite")
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
            conn = initialize_database(Path(tmp) / "macro.sqlite")
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

    def test_all_history_refresh_keeps_a_retraction_the_provider_does_not_redeliver(self) -> None:
        """Otherwise the next push restores the retracted row and undoes the decision."""

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            conn = initialize_database(database)
            try:
                series = get_series(conn, "jp.foreign_flows")
                week = observation(
                    series,
                    date(2026, 7, 3),
                    400343944.0,
                    datetime(2026, 7, 9, tzinfo=UTC),
                    period_days=4,
                )
                phantom = replace(
                    week,
                    observed_at=date(2026, 7, 9),
                    period_start=date(2026, 7, 9),
                    period_end=date(2026, 7, 9),
                    vintage_at=datetime(2026, 7, 10, tzinfo=UTC),
                )
                insert_observations(conn, [week, phantom])
                retract_observations(
                    conn,
                    "jp.foreign_flows",
                    [(date(2026, 7, 9), datetime(2026, 7, 10, tzinfo=UTC))],
                    vintage_at=datetime(2026, 7, 11, tzinfo=UTC),
                )
                conn.commit()
            finally:
                conn.close()

            with (
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[week],
                ),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = main(
                    [
                        "refresh",
                        "jp.foreign_flows",
                        "--all-history",
                        "--end",
                        "2026-07-20",
                        "--db",
                        str(database),
                    ]
                )

            conn = sqlite3.connect(database)
            conn.row_factory = sqlite3.Row
            try:
                retractions = conn.execute(
                    "SELECT COUNT(*) FROM observations WHERE fetch_status = ?",
                    (RETRACTED_STATUS,),
                ).fetchone()[0]
                observations = observations_in_range(
                    conn, "jp.foreign_flows", date(2026, 7, 1), date(2026, 7, 20)
                )
            finally:
                conn.close()

            self.assertEqual(exit_code, 0)
            self.assertEqual(retractions, 1)
            self.assertEqual([item.observed_at for item in observations], [date(2026, 7, 3)])

    def test_retraction_falls_back_to_the_vintage_underneath_it(self) -> None:
        """A faulty writer stamping a wrong value over a good one must not cost the good one."""

        with tempfile.TemporaryDirectory() as tmp:
            conn = initialize_database(Path(tmp) / "macro.sqlite")
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
            conn = initialize_database(Path(tmp) / "macro.sqlite")
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
            conn = initialize_database(Path(tmp) / "macro.sqlite")
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

    def test_retracting_an_input_refuses_while_a_derived_value_still_reads_it(self) -> None:
        """Recomputation cannot repair a derived row whose input date has gone."""

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
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

            with self.assertRaisesRegex(
                IndicatorsProviderError,
                r"would leave derived observations computed from it",
            ):
                service.retract("us.10y", [(date(2026, 7, 20), vintage)])

            conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                kept = observations_in_range(conn, "us.10y", date(2026, 7, 20), date(2026, 7, 20))
            finally:
                conn.close()

            self.assertEqual([item.value for item in kept], [4.5])

    def test_all_history_refresh_keeps_the_belief_a_retraction_put_back(self) -> None:
        """The restored row is an ordinary `ok` row, so only the vintage rule protects it."""

        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            conn = initialize_database(database)
            try:
                series = get_series(conn, "jp.foreign_flows")
                week = observation(
                    series,
                    date(2026, 7, 3),
                    400343944.0,
                    datetime(2026, 7, 9, tzinfo=UTC),
                    period_days=4,
                )
                wrong = replace(
                    week, value=200238561.0, vintage_at=datetime(2026, 7, 10, tzinfo=UTC)
                )
                insert_observations(conn, [week, wrong])
                retract_observations(
                    conn,
                    "jp.foreign_flows",
                    [(date(2026, 7, 3), datetime(2026, 7, 10, tzinfo=UTC))],
                    vintage_at=datetime(2026, 7, 26, tzinfo=UTC),
                )
                conn.commit()
            finally:
                conn.close()

            with (
                patch(
                    "baibai_engine.macro.indicators.service.fetch_observations",
                    return_value=[week],
                ),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = main(
                    [
                        "refresh",
                        "jp.foreign_flows",
                        "--all-history",
                        "--end",
                        "2026-07-20",
                        "--db",
                        str(database),
                    ]
                )

            conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                observations = observations_in_range(
                    conn, "jp.foreign_flows", date(2026, 7, 1), date(2026, 7, 20)
                )
            finally:
                conn.close()

            self.assertEqual(exit_code, 0)
            self.assertEqual([item.value for item in observations], [400343944.0])

    def test_retract_refuses_a_date_the_store_never_observed(self) -> None:
        """A retraction list that no longer matches the store is a stale list, not a no-op."""

        with tempfile.TemporaryDirectory() as tmp:
            conn = initialize_database(Path(tmp) / "macro.sqlite")
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
            conn = initialize_database(Path(tmp) / "macro.sqlite")
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
            database = Path(tmp) / "macro.sqlite"
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
            database = Path(tmp) / "macro.sqlite"
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
            downgrade_to_previous_schema(database)

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


class IndicatorsProviderParserTests(unittest.TestCase):
    def test_parse_fred_csv_filters_range_and_missing_values(self) -> None:
        series = _series("fred_csv", "DGS10")
        text = "observation_date,DGS10\n2026-05-01,4.39\n2026-05-02,.\n2026-05-04,4.45\n"

        observations = parse_fred_csv(
            series,
            text,
            start=date(2026, 5, 2),
            end=date(2026, 5, 5),
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 5, 4))
        self.assertEqual(observations[0].value, 4.45)

    def test_parse_umich_table_reads_the_column_for_each_published_month(self) -> None:
        series = _series("umich_sca", "ICS_ALL", unit="index", frequency="monthly")
        # The early history is quarterly, so consecutive rows can skip months.
        text = "Month,YYYY,ICS_ALL\nNovember,1952,86.2\nFebruary,1953,90.7\nJune,2026,49.5\n"

        observations = parse_umich_table(series, text, start=date(1952, 1, 1), end=date(2026, 7, 1))

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(1952, 11, 1), 86.2), (date(1953, 2, 1), 90.7), (date(2026, 6, 1), 49.5)],
        )

    def test_parse_umich_table_filters_to_the_requested_window(self) -> None:
        series = _series("umich_sca", "ICS_ALL", unit="index", frequency="monthly")
        text = "Month,YYYY,ICS_ALL\nApril,2026,49.8\nMay,2026,44.8\nJune,2026,49.5\n"

        observations = parse_umich_table(
            series, text, start=date(2026, 5, 1), end=date(2026, 5, 31)
        )

        self.assertEqual([item.observed_at for item in observations], [date(2026, 5, 1)])

    def test_parse_umich_table_skips_a_month_published_without_a_value(self) -> None:
        series = _series("umich_sca", "ICS_ALL", unit="index", frequency="monthly")
        text = "Month,YYYY,ICS_ALL\nMay,2026,44.8\nJune,2026,\n"

        observations = parse_umich_table(series, text, start=date(2026, 1, 1), end=date(2026, 7, 1))

        self.assertEqual([item.observed_at for item in observations], [date(2026, 5, 1)])

    def test_parse_umich_table_rejects_a_row_that_is_not_a_month_of_a_year(self) -> None:
        series = _series("umich_sca", "ICS_ALL", unit="index", frequency="monthly")
        text = "Month,YYYY,ICS_ALL\nQ2,2026,49.5\n"

        with self.assertRaisesRegex(IndicatorsProviderError, "not a month of a year"):
            parse_umich_table(series, text, start=date(2026, 1, 1), end=date(2026, 7, 1))

    def test_parse_umich_table_rejects_a_missing_value_column(self) -> None:
        series = _series("umich_sca", "ICS_ALL", unit="index", frequency="monthly")
        text = "Month,YYYY,ICE_ALL\nJune,2026,49.5\n"

        with self.assertRaisesRegex(IndicatorsProviderError, "missing column ICS_ALL"):
            parse_umich_table(series, text, start=date(2026, 1, 1), end=date(2026, 7, 1))

    def test_parse_h15_csv_computes_spread_bp(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B-RIFLGFCY02_N.B", unit="bp")
        text = "\n".join(
            [
                '"Series Description","2Y","10Y"',
                '"Time Period","RIFLGFCY02_N.B","RIFLGFCY10_N.B"',
                "2026-05-01,3.88,4.39",
                "2026-05-04,3.95,4.45",
            ]
        )

        observations = parse_h15_csv(
            series,
            text,
            start=date(2026, 5, 1),
            end=date(2026, 5, 4),
        )

        self.assertEqual(len(observations), 2)
        self.assertAlmostEqual(observations[0].value, 51.0)
        self.assertAlmostEqual(observations[1].value, 50.0)

    def test_parse_h15_csv_rejects_missing_required_column(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        text = '"Time Period",OTHER\n2026-05-01,4.39\n'

        with self.assertRaisesRegex(IndicatorsProviderError, "missing column RIFLGFCY10_N.B"):
            parse_h15_csv(series, text, start=date(2026, 5, 1), end=date(2026, 5, 1))

    def test_parse_h15_csv_reports_non_csv_response_with_snippet(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        text = "<!DOCTYPE html>\n<html><head><title>Access Denied</title></head></html>"

        with self.assertRaisesRegex(
            IndicatorsProviderError,
            r"missing Time Period header; response starts with: '<!DOCTYPE html> <html>",
        ):
            parse_h15_csv(series, text, start=date(2026, 5, 1), end=date(2026, 5, 1))

    def test_h15_fetch_sends_browser_user_agent(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        captured: dict[str, object] = {}

        class _Response:
            status_code = 200
            headers: dict[str, str] = {}

            def raise_for_status(self) -> None:
                return None

            def iter_content(self, chunk_size: int) -> object:
                del chunk_size
                return iter([b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n'])

            def close(self) -> None:
                return None

        class _Session:
            def get(self, url: str, **kwargs: object) -> _Response:
                captured["url"] = url
                captured["headers"] = kwargs.get("headers")
                return _Response()

        observations = FrbH15Provider().fetch(
            series,
            start=date(2026, 5, 1),
            end=date(2026, 5, 1),
            session=cast(HttpSession, _Session()),
        )

        self.assertEqual(len(observations), 1)
        headers = cast("dict[str, str]", captured["headers"])
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        # Companion browser headers reduce the datacenter-IP block-page rate.
        self.assertIn("Accept", headers)
        self.assertIn("Accept-Language", headers)

    def test_fetch_bytes_rejects_an_empty_body_naming_status_and_content_type(self) -> None:
        session = _StaticSession(
            _FakeResponse(b"", headers={"Content-Type": "text/html"}, status_code=200)
        )

        with self.assertRaisesRegex(
            IndicatorsProviderError,
            r"empty response body from https://example\.test/data "
            r"\(status 200, content-type 'text/html'\)",
        ):
            fetch_bytes(
                cast(HttpSession, session),
                "https://example.test/data",
                params=None,
                max_bytes=1000,
            )

    def test_fetch_bytes_keeps_an_empty_body_out_of_the_shared_cache(self) -> None:
        session = _StaticSession(_FakeResponse(b""))

        with FetchContext() as context:
            with self.assertRaises(SourceWithheldError):
                fetch_bytes(
                    cast(HttpSession, session),
                    "https://example.test/data",
                    params=None,
                    max_bytes=1000,
                    context=context,
                )

            # A cached empty body would fail every later series sharing the URL
            # without ever re-requesting it.
            self.assertEqual(context.bytes_cache, {})

    def test_fetch_text_reports_a_non_utf8_response_as_a_provider_failure(self) -> None:
        session = _StaticSession(_FakeResponse(b"\xff\xfe\x00garbage"))

        # A raw UnicodeDecodeError escapes the refresh's retry, which only knows
        # about provider errors, leaving the bytes cached for every later series.
        with self.assertRaisesRegex(IndicatorsProviderError, r"is not UTF-8"):
            fetch_text(
                cast(HttpSession, session),
                "https://example.test/data",
                params=None,
                max_bytes=1000,
            )

    def test_store_fetched_bytes_applies_the_guards_the_plain_path_applies(self) -> None:
        with FetchContext() as context:
            # A cache entry is read without any guard in front of it, so bytes
            # reaching the cache by another route owe the same checks.
            with self.assertRaises(SourceWithheldError):
                store_fetched_bytes(context, "https://example.test/data", None, b"", max_bytes=1000)
            with self.assertRaisesRegex(IndicatorsProviderError, r"response too large"):
                store_fetched_bytes(
                    context, "https://example.test/data", None, b"x" * 1001, max_bytes=1000
                )

            self.assertEqual(context.bytes_cache, {})

            store_fetched_bytes(context, "https://example.test/data", None, b"ok", max_bytes=1000)

            self.assertEqual(
                fetch_bytes(
                    cast(HttpSession, _RaisingSession()),
                    "https://example.test/data",
                    params=None,
                    max_bytes=1000,
                    context=context,
                ),
                b"ok",
            )

    def test_discard_cached_bytes_keeps_the_blocked_browser_record(self) -> None:
        with FetchContext() as context:
            store_fetched_bytes(context, "https://example.test/data", None, b"ok", max_bytes=1000)
            context.blocked_browser_urls["https://example.test/data"] = "turned away"

            context.discard_cached_bytes()

            # A retry is worth re-testing the plain request, not a navigation that
            # already spent a timeout failing against the same edge.
            self.assertEqual(context.bytes_cache, {})
            self.assertEqual(
                context.blocked_browser_urls, {"https://example.test/data": "turned away"}
            )

    def test_h15_falls_back_to_a_browser_when_the_plain_response_is_empty(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')

        with _context_with_browser(browser) as context:
            observations = FrbH15Provider().fetch(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(b""))),
                context=context,
            )

        self.assertEqual(len(observations), 1)
        self.assertAlmostEqual(observations[0].value, 4.39)
        self.assertEqual(len(browser.urls), 1)
        # The browser navigates to the same query the plain client sent.
        self.assertIn("series=bf17364827e38702b42a58cf8eaa3f78", browser.urls[0])
        self.assertIn("from=05%2F01%2F2026", browser.urls[0])

    def test_h15_falls_back_to_a_browser_when_the_plain_response_is_forbidden(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')

        with _context_with_browser(browser) as context:
            observations = FrbH15Provider().fetch(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                # A Cloudflare bot-mitigation challenge arrives as a 403 as
                # readily as it arrives as an empty body.
                session=cast(HttpSession, _StaticSession(_ForbiddenResponse())),
                context=context,
            )

        self.assertEqual(len(observations), 1)
        self.assertEqual(len(browser.urls), 1)

    def test_h15_falls_back_to_a_browser_when_the_plain_response_is_a_block_page(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')
        block_page = b"<!DOCTYPE html><html><title>Access Denied</title></html>"

        with _context_with_browser(browser) as context:
            observations = FrbH15Provider().fetch(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(block_page))),
                context=context,
            )

        self.assertEqual(len(observations), 1)
        self.assertEqual(len(browser.urls), 1)

    def test_h15_browser_fallback_downloads_once_for_every_series_in_a_pass(self) -> None:
        browser = _FakeBrowserFetcher(
            b'"Time Period","RIFLGFCY10_N.B","RIFLGFCY02_N.B"\n2026-05-01,4.39,3.88\n'
        )
        session = cast(HttpSession, _StaticSession(_FakeResponse(b"")))

        with _context_with_browser(browser) as context:
            for provider_series_id in ("RIFLGFCY10_N.B", "RIFLGFCY02_N.B"):
                FrbH15Provider().fetch(
                    _series("frb_h15", provider_series_id),
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                    session=session,
                    context=context,
                )

        # The package download is shared, so a second series reads the cache
        # rather than paying for another browser navigation.
        self.assertEqual(len(browser.urls), 1)

    def test_h15_reports_the_plain_failure_when_the_browser_is_also_blocked(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(IndicatorsProviderError("browser could not download"))

        with (
            _context_with_browser(browser) as context,
            self.assertRaisesRegex(
                IndicatorsProviderError,
                r"browser could not download; the plain client first failed with: "
                r"empty response body from .* \(status 200, content-type 'text/html'\)",
            ),
        ):
            FrbH15Provider().fetch(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(
                    HttpSession,
                    _StaticSession(_FakeResponse(b"", headers={"Content-Type": "text/html"})),
                ),
                context=context,
            )

    def test_h15_reports_the_block_page_when_the_browser_is_also_blocked(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(IndicatorsProviderError("browser could not download"))
        block_page = b"<!DOCTYPE html><html><title>Access Denied</title></html>"

        with (
            _context_with_browser(browser) as context,
            # A block page arrives as a normal 2xx, so the evidence of how the
            # edge blocked has to survive the fallback just like an empty body.
            self.assertRaisesRegex(
                IndicatorsProviderError,
                r"the plain client first failed with: response without the CSV header: "
                r"'<!DOCTYPE html><html><title>Access Denied",
            ),
        ):
            FrbH15Provider().fetch(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(block_page))),
                context=context,
            )

    def test_h15_rejects_a_browser_response_without_the_csv_header(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(b"<!DOCTYPE html><html><title>Just a moment</title></html>")

        with _context_with_browser(browser) as context:
            with self.assertRaisesRegex(
                IndicatorsProviderError, r"browser response without the CSV header"
            ):
                FrbH15Provider().fetch(
                    series,
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                    session=cast(HttpSession, _StaticSession(_FakeResponse(b""))),
                    context=context,
                )

            # An unvalidated browser answer in the cache would fail every later
            # series sharing the URL with the parser's format error.
            self.assertEqual(context.bytes_cache, {})

    def test_h15_rejects_a_browser_response_that_is_not_utf8(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(b"\xff\xfe\x00garbage")

        with _context_with_browser(browser) as context:
            # A decode error is not an IndicatorsProviderError, so escaping as one
            # would deny the refresh its retry and leave the bytes cached.
            with self.assertRaises(IndicatorsProviderError):
                FrbH15Provider().fetch(
                    series,
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                    session=cast(HttpSession, _StaticSession(_FakeResponse(b""))),
                    context=context,
                )

            self.assertEqual(context.bytes_cache, {})

    def test_h15_does_not_re_navigate_after_the_browser_was_blocked_in_this_pass(self) -> None:
        browser = _FakeBrowserFetcher(IndicatorsProviderError("navigation timed out"))
        session = cast(HttpSession, _StaticSession(_FakeResponse(b"")))
        reported: list[str] = []

        with _context_with_browser(browser) as context:
            for provider_series_id in ("RIFLGFCY10_N.B", "RIFLGFCY02_N.B"):
                with self.assertRaises(IndicatorsProviderError) as caught:
                    FrbH15Provider().fetch(
                        _series("frb_h15", provider_series_id),
                        start=date(2026, 5, 1),
                        end=date(2026, 5, 1),
                        session=session,
                        context=context,
                    )
                reported.append(str(caught.exception))

        # Each navigation costs a timeout, so a blocked edge must be paid for once
        # per pass rather than once per series and attempt.
        self.assertEqual(len(browser.urls), 1)
        # The series that skipped the navigation is the only one still reporting,
        # so what the edge answered has to travel with the record of the block.
        for message in reported:
            self.assertIn("navigation timed out", message)

    def test_h15_retries_the_browser_after_it_could_not_be_started(self) -> None:
        browser = _FakeBrowserFetcher(BrowserUnavailableError("failed to launch"))
        session = cast(HttpSession, _StaticSession(_FakeResponse(b"")))
        reached_browser = 0

        with _context_with_browser(browser) as context:
            for provider_series_id in ("RIFLGFCY10_N.B", "RIFLGFCY02_N.B"):
                with self.assertRaises(IndicatorsProviderError):
                    FrbH15Provider().fetch(
                        _series("frb_h15", provider_series_id),
                        start=date(2026, 5, 1),
                        end=date(2026, 5, 1),
                        session=session,
                        context=context,
                    )
                reached_browser = len(browser.urls)

            # A browser that never started says nothing about the source, so the
            # next series has the same reason to try as this one did.
            self.assertEqual(context.blocked_browser_urls, {})

        self.assertEqual(reached_browser, 2)

    def test_browser_launch_failure_is_reported_as_the_browser_being_unavailable(self) -> None:
        # The provider's decision not to record a block hangs on this type, so it
        # is fixed where it is raised rather than only where a fake supplies it.
        # Imported here so Playwright stays off the whole suite's import path,
        # the same reason the production launch is lazy.
        from playwright.sync_api import Error as PlaywrightError

        from baibai_engine.macro.indicators.providers.browser import BrowserFetcher

        with (
            patch(
                "baibai_engine.macro.indicators.providers.browser.sync_playwright",
                side_effect=PlaywrightError("no browser binary"),
            ),
            self.assertRaisesRegex(BrowserUnavailableError, r"failed to launch headless browser"),
        ):
            BrowserFetcher().fetch_download("https://example.test/data", max_bytes=1000)

    def test_browser_launch_failure_on_a_full_disk_is_still_a_provider_failure(self) -> None:
        from baibai_engine.macro.indicators.providers.browser import BrowserFetcher

        with (
            patch(
                "baibai_engine.macro.indicators.providers.browser.tempfile.TemporaryDirectory",
                side_effect=OSError("No space left on device"),
            ),
            # A bare OSError would escape every handler that knows what a provider
            # failure means, taking the refresh's retry and cache discard with it.
            self.assertRaisesRegex(BrowserUnavailableError, r"failed to launch headless browser"),
        ):
            BrowserFetcher().fetch_download("https://example.test/data", max_bytes=1000)

    def test_browser_launch_receives_only_allowlisted_runtime_environment(self) -> None:
        from baibai_engine.macro.indicators.providers.browser import BrowserFetcher

        context = MagicMock()
        playwright = MagicMock()
        playwright.chromium.launch_persistent_context.return_value = context
        starter = MagicMock()
        starter.start.return_value = playwright
        inherited = {
            "HOME": "/home/runner",
            "PATH": "/usr/bin",
            "XDG_RUNTIME_DIR": "/run/user/1001",
            "PLAYWRIGHT_BROWSERS_PATH": "/home/runner/.cache/ms-playwright",
            "JQUANTS_API_KEY": "jquants-secret",
            "EDINET_API_KEY": "edinet-secret",
            "R2_SECRET_ACCESS_KEY": "r2-secret",
            "FUTURE_VENDOR_TOKEN": "future-secret",
            "HTTPS_PROXY": "https://proxy-user:proxy-secret@example.test",
        }

        with (
            patch(
                "baibai_engine.macro.indicators.providers.browser.sync_playwright",
                return_value=starter,
            ),
            patch.dict("os.environ", inherited, clear=True),
            BrowserFetcher() as fetcher,
        ):
            assert fetcher._ensure_context() is context

        launch = playwright.chromium.launch_persistent_context
        assert launch.call_count == 1
        assert launch.call_args.kwargs["env"] == {
            "HOME": "/home/runner",
            "PATH": "/usr/bin",
            "XDG_RUNTIME_DIR": "/run/user/1001",
            "PLAYWRIGHT_BROWSERS_PATH": "/home/runner/.cache/ms-playwright",
        }

    def test_h15_does_not_fall_back_to_a_browser_for_a_failure_that_is_not_a_block(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')
        oversized = _FakeResponse(b"x" * 32, headers={"Content-Length": "9000000"})

        with _context_with_browser(browser) as context:
            with self.assertRaisesRegex(IndicatorsProviderError, r"response too large"):
                FrbH15Provider().fetch(
                    series,
                    start=date(2026, 5, 1),
                    end=date(2026, 5, 1),
                    session=cast(HttpSession, _StaticSession(oversized)),
                    context=context,
                )

            # The size cap did its job; re-fetching the same resource through a
            # browser would have it written to disk with no cap in front of it.
            self.assertEqual(browser.urls, [])

    def test_h15_browser_navigation_preserves_a_query_already_in_the_source_url(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        series = replace(series, source_url="https://example.test/Output.aspx?rel=H15")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')

        with _context_with_browser(browser) as context:
            FrbH15Provider().fetch(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(b""))),
                context=context,
            )

        # The plain client merges params into an existing query; the navigation
        # must address the same resource rather than a doubled "?".
        self.assertEqual(browser.urls[0].count("?"), 1)
        self.assertIn("rel=H15", browser.urls[0])
        self.assertIn("layout=seriescolumn", browser.urls[0])

    def test_h15_browser_download_is_capped_by_the_provider_limit(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        browser = _FakeBrowserFetcher(b'"Time Period","RIFLGFCY10_N.B"\n2026-05-01,4.39\n')

        with _context_with_browser(browser) as context:
            FrbH15Provider().fetch(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(b""))),
                context=context,
            )

        # One resource, one ceiling — not a second one that depends on the route.
        self.assertEqual(browser.max_bytes, [MAX_CSV_RESPONSE_BYTES])

    def test_h15_without_a_context_reports_the_response_instead_of_falling_back(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        block_page = b"<!DOCTYPE html><html><title>Access Denied</title></html>"

        with self.assertRaisesRegex(
            IndicatorsProviderError, r"missing Time Period header; response starts with: '<!DOCTYPE"
        ):
            FrbH15Provider().fetch(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=cast(HttpSession, _StaticSession(_FakeResponse(block_page))),
            )

    def test_parse_ecb_fx_csv_computes_cross_rate(self) -> None:
        series = _series("ecb_fx", "USDJPY", unit="jpy-per-usd")
        text = "Date,USD,JPY,AUD\n2026-05-08,1.1761,184.37,1.6259\n"

        observations = parse_ecb_fx_csv(
            series,
            text,
            start=date(2026, 5, 8),
            end=date(2026, 5, 8),
        )

        self.assertEqual(len(observations), 1)
        self.assertAlmostEqual(observations[0].value, 156.76, places=2)

    def test_parse_ecb_fx_csv_rejects_missing_required_column(self) -> None:
        series = _series("ecb_fx", "USDJPY", unit="jpy-per-usd")
        text = "Date,JPY\n2026-05-08,184.37\n"

        with self.assertRaisesRegex(IndicatorsProviderError, "missing column"):
            parse_ecb_fx_csv(series, text, start=date(2026, 5, 8), end=date(2026, 5, 8))

    def test_fetch_observations_wraps_request_exception(self) -> None:
        series = _series("fred_csv", "DGS10")

        with self.assertRaisesRegex(IndicatorsProviderError, "failed to fetch"):
            fetch_observations(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=_RaisingSession(),
            )

    def test_fetch_observations_rejects_oversized_response(self) -> None:
        series = _series("fred_csv", "DGS10")

        with self.assertRaisesRegex(IndicatorsProviderError, "too large"):
            fetch_observations(
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

    # One release phrasing per row: the case name, the release text, the month the
    # reading is wanted for, the month the release was published, and the value that
    # must come back — None where the text states no reading for that month. The rows
    # are the whole point: each is a phrasing a real S&P Global release used, and the
    # extractor has to keep answering all of them.
    _READING_CASES: tuple[tuple[str, str, date, date, float | None], ...] = (
        (
            "reads_headline_for_month",
            "the headline S&P Global Japan Manufacturing PMI picked up to 54.8 in June from 54.5 in May and signalled an improvement in operating conditions.",
            date(2026, 6, 1),
            date(2026, 6, 1),
            54.8,
        ),
        (
            "returns_none_when_month_absent",
            "the headline PMI picked up to 54.8 in June from 54.5 in May.",
            date(2026, 3, 1),
            date(2026, 6, 1),
            None,
        ),
        (
            # Services releases phrase the value as "the headline index posted X in
            # Month" — the value sentence names no "PMI", and a definitional
            # "the headline figure is ..." sentence comes first.
            "reads_services_headline_without_pmi_token",
            "The headline figure is the Services Business Activity Index, which tracks changes in the volume of business activity. A reading above 50.0 indicates growth. The headline index posted 53.2 in November, up fractionally from 53.1 in October and signalled a further solid expansion.",
            date(2025, 11, 1),
            date(2025, 11, 1),
            53.2,
        ),
        (
            # Older releases drop "the headline" and lead with the index name.
            "reads_index_anchored_statement_without_headline",
            "The seasonally adjusted S&P Global US Services PMI® Business Activity Index posted 52.9 in January, down markedly from 56.8 in December.",
            date(2025, 1, 1),
            date(2025, 1, 1),
            52.9,
        ),
        (
            # "posted at the neutral level of 50.0 in October" — a qualifier sits
            # between the reporting verb and the number.
            "reads_value_with_qualifier_between_verb_and_number",
            "The seasonally adjusted S&P Global US Manufacturing Purchasing Managers' Index™ (PMI) posted at the neutral level of 50.0 in October, in line with the earlier flash estimate.",
            date(2023, 10, 1),
            date(2023, 10, 1),
            50.0,
        ),
        (
            # A release states the month before it as well as its own, which is how a
            # month whose own release is unavailable is read.
            "reads_the_previous_month_named_in_a_restating_release",
            "The headline index posted 53.2 in November, up fractionally from 53.1 in October and signalled a further solid expansion.",
            date(2025, 10, 1),
            date(2025, 11, 1),
            53.1,
        ),
        (
            # The comparison names no month, so it can only be read for the month before
            # the one the release reports.
            "reads_the_previous_month_from_a_bare_comparison",
            "The seasonally adjusted S&P Global US Manufacturing Purchasing Managers’ Index™ (PMI®) remained above the crucial 50.0 no-change mark in March. Recording 50.2, down from 52.7, the PMI signaled a marginal improvement.",
            date(2025, 2, 1),
            date(2025, 3, 1),
            52.7,
        ),
        (
            # The statement introduces its reading without naming a month, so it belongs
            # to the release's own month and must not answer for the month before it.
            "does_not_attribute_a_release_reading_to_the_previous_month",
            "The headline index posted 53.2 in November, ending a soft patch.",
            date(2025, 10, 1),
            date(2025, 11, 1),
            None,
        ),
        (
            # pypdf renders "47.9" as "47 .9" in some releases.
            "reads_a_value_the_pdf_text_split_at_the_decimal",
            "The PMI fell to 47 .9 in August, from 49.0 in July, indicating a downturn.",
            date(2023, 8, 1),
            date(2023, 8, 1),
            47.9,
        ),
        (
            # "in <month> to <value>" — the month leads the value in the same clause.
            "reads_a_month_stated_before_its_value",
            "The seasonally adjusted S&P Global US Services PMI ® Business Activity Index fell for the third month running in April to 51.3 from 51.7 in March.",
            date(2024, 4, 1),
            date(2024, 4, 1),
            51.3,
        ),
        (
            # "during <month>", and the flash estimate in the same sentence is provisional.
            "reads_a_value_stated_during_the_month",
            "The S&P Global US Services PMI® Business Activity Index recorded 53.7 during May, which was stronger than the earlier 'flash' reading of 52.3.",
            date(2025, 5, 1),
            date(2025, 5, 1),
            53.7,
        ),
        (
            # The statement sentence gives only the no-change threshold; the reading
            # follows in the next sentence.
            "reads_the_level_stated_after_the_threshold_sentence",
            "The headline au Jibun Bank Japan Services Business Activity Index remained above the 50.0 no-change mark for the fourteenth successive month in October, signalling a further expansion. That said, at 51.6 the index was down from 53.8 in September and pointed to a modest rise in output.",
            date(2023, 10, 1),
            date(2023, 10, 1),
            51.6,
        ),
        (
            # "slipped from <previous> in <previous month> to <reading>".
            "reads_a_movement_destination_stated_after_a_comparison",
            "However, the headline index slipped from 53.2 in November to 51.6, to signal a modest rate of growth that was the slowest seen since May.",
            date(2025, 12, 1),
            date(2025, 12, 1),
            51.6,
        ),
        (
            # "reaching a 33-month high of <reading> following a reading of <previous>".
            "reads_a_reading_named_as_a_record_level",
            "The seasonally adjusted S&P Global US Services PMI® Business Activity Index rose for the second month running in December, reaching a 33-month high of 56.8 following a reading of 56.1 in November.",
            date(2024, 12, 1),
            date(2024, 12, 1),
            56.8,
        ),
        (
            # A reading of exactly 50.0 is stated as equal to the threshold.
            "reads_a_reading_equal_to_the_no_change_mark",
            "The seasonally adjusted S&P Global US Manufacturing Purchasing Managers’ Index™ (PMI ®) posted in line with the 50.0 no-change mark in April to point to stable business conditions.",
            date(2024, 4, 1),
            date(2024, 4, 1),
            50.0,
        ),
        (
            # "broadly in line with" states an approximation, not the reading.
            "ignores_a_hedged_comparison_with_the_no_change_mark",
            "The seasonally adjusted S&P Global US Manufacturing Purchasing Managers’ Index™ (PMI ®) was broadly in line with the 50.0 no-change mark in April.",
            date(2024, 4, 1),
            date(2024, 4, 1),
            None,
        ),
        (
            # "below the 50.0 no-change mark in November" is the threshold; the reading is
            # the level stated beside it.
            "ignores_a_threshold_the_reading_is_measured_against",
            "The seasonally adjusted S&P Global US Manufacturing Purchasing Managers’ Index™ (PMI®) remained below the 50.0 no-change mark in November, but at 49.7 pointed to only a marginal worsening in the health of the sector.",
            date(2024, 11, 1),
            date(2024, 11, 1),
            49.7,
        ),
        (
            # A span average is not any single month's reading.
            "ignores_an_average_over_several_months",
            "The Business Activity Index has trended at 53.7 from January to November, comfortably above the next-highest annual average of 52.4 set in 2013.",
            date(2023, 11, 1),
            date(2023, 11, 1),
            None,
        ),
        (
            # The composite index is published in the same release and its statement reads
            # like the headline one.
            "ignores_the_composite_index_statement",
            "S&P Global US Services PMI® At 50.7 in November, the final S&P Global US Composite PMI Output Index* was unchanged from October. The headline S&P Global US Services PMI® Business Activity Index recorded 54.1 in November.",
            date(2025, 11, 1),
            date(2025, 11, 1),
            54.1,
        ),
        (
            # A sub-index moves on its own and must never answer for the headline.
            "ignores_a_sub_index_statement",
            "The headline index posted 49.7 in November, a marginal worsening. The New Orders Index rose to 48.2 in November.",
            date(2024, 11, 1),
            date(2024, 11, 1),
            49.7,
        ),
        (
            # Only a sentence that refers back to the index carries the statement on, so a
            # sentence about another subject cannot supply the headline reading.
            "ignores_a_sentence_that_moves_on_from_the_statement",
            "The headline index remained subdued in June. Employment growth eased to 51.2 in June.",
            date(2026, 6, 1),
            date(2026, 6, 1),
            None,
        ),
        (
            # "compared to <previous>" borrows the preposition a movement uses, without
            # stating the release's own reading.
            "ignores_a_comparison_sharing_the_movement_preposition",
            "The headline index improved in October, compared to 52.0 in September.",
            date(2025, 10, 1),
            date(2025, 10, 1),
            None,
        ),
        (
            # A chart caption pairs the month with a year, which is not a reading.
            "ignores_a_year_beside_the_month",
            "Comment January 2026 Index, sa, >50 = growth m/m. The headline index eased.",
            date(2026, 1, 1),
            date(2026, 1, 1),
            None,
        ),
    )

    # Text the extractor must refuse rather than answer, with the reason it gives.
    _REFUSAL_CASES: tuple[tuple[str, str, date, date, str], ...] = (
        (
            "rejects_implausible_reading",
            "the headline PMI surged to 101.0 in June, an unprecedented reading.",
            date(2026, 6, 1),
            date(2026, 6, 1),
            "outside plausible range",
        ),
        (
            "rejects_conflicting_values",
            "the headline PMI reading was 54.8 in June. Separately, the headline PMI figure was 55.9 in June per a revised estimate.",
            date(2026, 6, 1),
            date(2026, 6, 1),
            "conflicting",
        ),
    )

    def test_extract_pmi_value_reads_the_phrasings_releases_actually_use(self) -> None:
        for name, text, expected_at, release_at, expected in self._READING_CASES:
            with self.subTest(case=name):
                self.assertEqual(
                    extract_pmi_value(
                        text,
                        expected_observed_at=expected_at,
                        release_observed_at=release_at,
                    ),
                    expected,
                )

    def test_extract_pmi_value_refuses_text_it_cannot_read_one_reading_from(self) -> None:
        for name, text, expected_at, release_at, reason in self._REFUSAL_CASES:
            with self.subTest(case=name), self.assertRaisesRegex(PmiExtractionError, reason):
                extract_pmi_value(
                    text,
                    expected_observed_at=expected_at,
                    release_observed_at=release_at,
                )

    def test_extract_pmi_value_accepts_full_diffusion_index_domain(self) -> None:
        for expected in (0.0, 9.9, 21.5, 70.4, 100.0):
            with self.subTest(expected=expected):
                value = extract_pmi_value(
                    f"the headline PMI posted {expected:.1f} in June.",
                    expected_observed_at=date(2026, 6, 1),
                    release_observed_at=date(2026, 6, 1),
                )

                self.assertEqual(value, expected)

    def test_extract_pdf_text_rejects_non_pdf(self) -> None:
        with self.assertRaisesRegex(IndicatorsProviderError, "not a PDF"):
            extract_pdf_text(b"<html>blocked</html>")

    def test_spglobal_pmi_falls_back_to_a_browser_when_the_release_is_withheld(self) -> None:
        browser = _FakeBrowserFetcher(IndicatorsProviderError("browser was blocked too"))

        with (
            _context_with_browser(browser) as context,
            self.assertRaisesRegex(IndicatorsProviderError, r"browser was blocked too"),
        ):
            release_text(
                "https://www.pmi.spglobal.com/Public/Home/PressRelease/" + "a" * 32,
                session=cast(HttpSession, _StaticSession(_ForbiddenResponse())),
                context=context,
            )

        self.assertEqual(len(browser.urls), 1)
        # A WAF-gated month always takes this route, so the browser must carry the
        # provider's own ceiling rather than the fetcher's more generous default.
        self.assertEqual(browser.max_bytes, [MAX_PMI_PDF_BYTES])

    def test_spglobal_pmi_does_not_fall_back_for_a_failure_that_is_not_a_block(self) -> None:
        browser = _FakeBrowserFetcher(b"%PDF-1.4 unused")
        oversized = _FakeResponse(b"x" * 32, headers={"Content-Length": "9000000"})

        with (
            _context_with_browser(browser) as context,
            self.assertRaisesRegex(IndicatorsProviderError, r"response too large"),
        ):
            release_text(
                "https://www.pmi.spglobal.com/Public/Home/PressRelease/" + "a" * 32,
                session=cast(HttpSession, _StaticSession(oversized)),
                context=context,
            )

        # The size cap did its job; routing the same resource through a browser
        # would have it written to disk with no cap ahead of it.
        self.assertEqual(browser.urls, [])

    def test_spglobal_pmi_manifest_parses_jp_manufacturing_stream(self) -> None:
        streams = load_manifest(MANIFEST_PATH)

        self.assertIn("jp_manufacturing", streams)
        entries = streams["jp_manufacturing"]
        self.assertGreaterEqual(len(entries), 36)
        # entries are month-sorted and every URL matches the official release pattern
        observed = [entry.observed_at for entry in entries]
        self.assertEqual(observed, sorted(observed))
        for entry in entries:
            self.assertRegex(
                entry.url,
                r"https://www\.pmi\.spglobal\.com/Public/Home/PressRelease/[0-9a-f]{32}",
            )

    def test_spglobal_pmi_all_history_start_reaches_the_oldest_manifest_month(self) -> None:
        # `--all-history` clips to the provider's declared start, so a manifest month
        # older than that start would be unfetchable by the standard rebuild path.
        oldest = min(
            entry.observed_at
            for entries in load_manifest(MANIFEST_PATH).values()
            for entry in entries
        )

        self.assertLessEqual(SpGlobalPmiProvider.spec.all_history_start, oldest)

    def test_spglobal_pmi_manifest_rejects_duplicate_month(self) -> None:
        url = "https://www.pmi.spglobal.com/Public/Home/PressRelease/" + "a" * 32
        entries = [
            {"observed_at": "2026-05-01", "url": url},
            {"observed_at": "2026-05-01", "url": url},
        ]

        with self.assertRaisesRegex(IndicatorsProviderError, "duplicate month"):
            _parse_stream("jp_manufacturing", entries)

    def test_spglobal_pmi_manifest_rejects_bad_url(self) -> None:
        entries = [{"observed_at": "2026-05-01", "url": "https://evil.example/x"}]

        with self.assertRaisesRegex(IndicatorsProviderError, "invalid release URL"):
            _parse_stream("jp_manufacturing", entries)

    def test_spglobal_pmi_manifest_covers_every_registered_pmi_series(self) -> None:
        # Every registered spglobal_pmi series must resolve to a manifest stream, so a
        # newly registered PMI series can never ship without its release URLs.
        from baibai_engine.macro.indicators.definitions import load_definitions

        streams = set(load_manifest(MANIFEST_PATH))
        pmi_series = [s for s in load_definitions().series if s.provider == "spglobal_pmi"]
        self.assertTrue(pmi_series)
        for series in pmi_series:
            self.assertIn(
                series.provider_series_id,
                streams,
                f"{series.series_id} has no manifest stream {series.provider_series_id!r}",
            )

    def test_spglobal_pmi_downloads_only_the_months_the_store_is_missing(self) -> None:
        # Each month costs one PDF download, so an incremental refresh fetches only
        # what the store is missing while still returning the whole window (a
        # partial window would report the series as having no data for it).
        series = _series("spglobal_pmi", "jp_manufacturing", unit="index", frequency="monthly")
        stream = _pmi_stream(["2026-04-01", "2026-05-01", "2026-06-01"])
        stored = _pmi_stored(series.series_id, stream[:2], (49.5, 50.1))

        observations, fetched = _fetch_pmi_stream(
            series,
            stream,
            start=date(2026, 4, 1),
            end=date(2026, 6, 30),
            context=FetchContext(store_reader=lambda *_: stored, purpose="refresh"),
        )

        self.assertEqual(fetched, [stream[2].url])
        self.assertEqual(
            [(entry.observed_at, entry.value) for entry in observations],
            [
                (date(2026, 4, 1), 49.5),
                (date(2026, 5, 1), 50.1),
                (date(2026, 6, 1), 50.4),
            ],
        )

    def test_spglobal_pmi_rebuild_refetches_every_stored_month(self) -> None:
        series = _series("spglobal_pmi", "jp_manufacturing", unit="index", frequency="monthly")
        stream = _pmi_stream(["2026-04-01", "2026-05-01", "2026-06-01"])
        stored = _pmi_stored(series.series_id, stream[:2], (49.5, 50.1))

        observations, fetched = _fetch_pmi_stream(
            series,
            stream,
            start=date(2026, 4, 1),
            end=date(2026, 6, 30),
            context=FetchContext(store_reader=lambda *_: stored, purpose="rebuild"),
        )

        self.assertEqual(len(observations), 3)
        self.assertEqual(fetched, [release.url for release in stream])

    def test_spglobal_pmi_refetches_a_month_whose_release_url_changed(self) -> None:
        # Correcting a release URL in the manifest must reach the store; skipping by
        # month alone would leave the value the superseded URL produced.
        series = _series("spglobal_pmi", "jp_manufacturing", unit="index", frequency="monthly")
        stream = _pmi_stream(["2026-05-01", "2026-06-01"])
        superseded = ObservationRecord(
            series_id=series.series_id,
            observed_at=date(2026, 5, 1),
            value=50.1,
            unit="index",
            source_url="https://www.pmi.spglobal.com/Public/Home/PressRelease/" + "f" * 32,
            vintage_at=datetime.now(UTC),
        )
        stored = (superseded, *_pmi_stored(series.series_id, stream[1:], (50.4,)))

        observations, fetched = _fetch_pmi_stream(
            series,
            stream,
            start=date(2026, 5, 1),
            end=date(2026, 6, 30),
            context=FetchContext(store_reader=lambda *_: stored, purpose="refresh"),
        )

        self.assertEqual(fetched, [stream[0].url])
        may = next(entry for entry in observations if entry.observed_at == date(2026, 5, 1))
        self.assertEqual(may.source_url, stream[0].url)

    def test_spglobal_pmi_read_is_not_blocked_by_a_stale_manifest(self) -> None:
        # A read must still answer from what the store holds: failing it would make a
        # late manifest hide months that were already collected.
        series = _series("spglobal_pmi", "jp_manufacturing", unit="index", frequency="monthly")
        stream = _pmi_stream(["2026-05-01", "2026-06-01"])
        stored = _pmi_stored(series.series_id, stream[1:], (50.4,))

        observations, fetched = _fetch_pmi_stream(
            series,
            stream,
            start=date(2026, 6, 1),
            end=date(2026, 8, 10),
            context=FetchContext(store_reader=lambda *_: stored, purpose="read"),
        )

        self.assertEqual(fetched, [])
        self.assertEqual([entry.observed_at for entry in observations], [date(2026, 6, 1)])

    def test_spglobal_pmi_rejects_a_manifest_behind_the_release_calendar(self) -> None:
        # A month missing from the hand-maintained manifest would otherwise fetch
        # nothing and leave the series stale without any error.
        series = _series("spglobal_pmi", "jp_manufacturing", unit="index", frequency="monthly")
        stream = _pmi_stream(["2026-05-01", "2026-06-01"])

        with (
            patch.object(spglobal_pmi, "load_manifest", return_value={"jp_manufacturing": stream}),
            self.assertRaisesRegex(
                IndicatorsProviderError,
                r"manifest for jp_manufacturing ends at 2026-06-01 .*expects 2026-07-01",
            ),
        ):
            SpGlobalPmiProvider().fetch(
                series,
                start=date(2026, 7, 1),
                end=date(2026, 8, 10),
                session=cast(HttpSession, _RaisingSession()),
            )

    def test_spglobal_pmi_accepts_a_manifest_inside_the_release_grace_window(self) -> None:
        # Before the grace day the newest release may not be published yet, so the
        # guard must not fail for data that cannot exist.
        series = _series("spglobal_pmi", "jp_manufacturing", unit="index", frequency="monthly")
        stream = _pmi_stream(["2026-05-01", "2026-06-01"])

        observations, fetched = _fetch_pmi_stream(
            series,
            stream,
            start=date(2026, 7, 1),
            end=date(2026, 8, 5),
            context=None,
        )

        self.assertEqual(observations, [])
        self.assertEqual(fetched, [])

    def test_derived_net_liquidity_formula_converts_units(self) -> None:
        value = FORMULAS["us.net_liquidity"].evaluate(
            {"us.fed_assets": 6_600_000.0, "us.reverse_repo": 500_000.0, "us.tga": 700_000.0}
        )

        # (6,600,000 - 500,000 - 700,000) / 1000 = 5400.0 (USD million -> USD billion)
        self.assertEqual(value, 5400.0)

    def test_derived_rate_diff_formula(self) -> None:
        value = FORMULAS["rate_diff.us_jp_10y"].evaluate({"us.10y": 4.55, "jp.10y": 2.715})

        self.assertAlmostEqual(value, 1.835, places=3)

    def test_derived_policy_path_gap_formulas(self) -> None:
        """A 2Y yield above the policy rate is tightening priced; below it, easing."""

        tightening = FORMULAS["us.policy_path_gap"].evaluate(
            {"us.2y": 4.37, "us.fed_funds.upper": 3.75, "us.fed_funds.lower": 3.5}
        )
        easing = FORMULAS["us.policy_path_gap"].evaluate(
            {"us.2y": 3.88, "us.fed_funds.upper": 4.5, "us.fed_funds.lower": 4.25}
        )
        normalization = FORMULAS["jp.policy_path_gap"].evaluate(
            {"jp.2y": 1.45, "jp.policy_rate": 0.978}
        )

        assert tightening is not None
        assert easing is not None
        assert normalization is not None
        self.assertAlmostEqual(tightening, 0.745, places=3)
        self.assertAlmostEqual(easing, -0.495, places=3)
        self.assertAlmostEqual(normalization, 0.472, places=3)

    def test_derived_policy_path_gap_rejects_an_impossible_spread(self) -> None:
        with self.assertRaisesRegex(DerivedComputationError, "outside plausible range"):
            FORMULAS["jp.policy_path_gap"].evaluate({"jp.2y": 12.0, "jp.policy_rate": 0.0})

    def test_derived_terms_of_trade_formula(self) -> None:
        value = FORMULAS["jp.terms_of_trade"].evaluate(
            {"jp.export_price_index": 162.6, "jp.import_price_index": 196.6}
        )

        # 162.6 / 196.6 = 0.8271: export prices below import prices (yen-weak cost)
        assert value is not None
        self.assertAlmostEqual(value, 0.8271, places=4)

    def test_derived_jp_erp_formula(self) -> None:
        value = FORMULAS["jp.erp"].evaluate({"jp.nikkei_per": 17.82, "jp.10y": 1.7})

        # 100 / 17.82 - 1.7 = 3.9117: Nikkei earnings yield minus the 10Y JGB
        assert value is not None
        self.assertAlmostEqual(value, 3.9117, places=3)

    def test_derived_formula_rejects_out_of_range(self) -> None:
        with self.assertRaisesRegex(DerivedComputationError, "outside plausible range"):
            FORMULAS["us.erp"].evaluate({"us.sp500_earnings_yield": 99.0, "us.10y": 4.0})

    def test_derived_gold_copper_skips_zero_divisor(self) -> None:
        self.assertIsNone(FORMULAS["gold_copper_ratio"].evaluate({"gold": 3000.0, "copper": 0.0}))

    def test_derived_provider_aligns_inputs_and_skips_partial_dates(self) -> None:
        series = _series("derived", "rate_diff.us_jp_10y", unit="percent")
        store: dict[str, tuple[ObservationRecord, ...]] = {
            "us.10y": (
                observation("us.10y", date(2026, 7, 16), 4.53),
                observation("us.10y", date(2026, 7, 17), 4.55),
            ),
            # jp.10y missing 07-16 -> that date is skipped (no half-computed value)
            "jp.10y": (observation("jp.10y", date(2026, 7, 17), 2.715),),
        }
        context = FetchContext(store_reader=lambda sid, s, e: store[sid])

        observations = DerivedProvider().fetch(
            series,
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
            session=cast(HttpSession, object()),
            context=context,
        )

        self.assertEqual([obs.observed_at for obs in observations], [date(2026, 7, 17)])
        self.assertAlmostEqual(observations[0].value, 1.835, places=3)

    def test_derived_provider_requires_store_reader(self) -> None:
        series = _series("derived", "rate_diff.us_jp_10y", unit="percent")

        with self.assertRaisesRegex(IndicatorsProviderError, "store reader"):
            DerivedProvider().fetch(
                series,
                start=date(2026, 7, 1),
                end=date(2026, 7, 31),
                session=cast(HttpSession, object()),
                context=FetchContext(),
            )

    def test_derived_real_10y_proxy_formula(self) -> None:
        value = FORMULAS["jp.real_10y_proxy"].evaluate({"jp.10y": 1.62, "jp.cpi.core_yoy": 1.6})

        # 1.62 - 1.6 = 0.02: the nominal 10Y JGB barely clears core inflation
        assert value is not None
        self.assertAlmostEqual(value, 0.02, places=3)

    def test_derived_us_erp_uses_monthly_alignment(self) -> None:
        self.assertEqual(FORMULAS["us.erp"].alignment, "monthly")
        self.assertEqual(FORMULAS["us.erp"].monthly_observation_date, "latest_input")

    def test_derived_provider_monthly_alignment_uses_month_end_of_daily_input(self) -> None:
        series = _series("derived", "jp.real_10y_proxy", unit="percent", frequency="monthly")
        store: dict[str, tuple[ObservationRecord, ...]] = {
            "jp.10y": (
                observation("jp.10y", date(2026, 5, 1), 1.50),
                observation("jp.10y", date(2026, 5, 29), 1.58),
                observation("jp.10y", date(2026, 6, 1), 1.60),
                observation("jp.10y", date(2026, 6, 30), 1.62),  # June month-end reading
            ),
            "jp.cpi.core_yoy": (
                observation("jp.cpi.core_yoy", date(2026, 5, 1), 1.5),
                observation("jp.cpi.core_yoy", date(2026, 6, 1), 1.6),
            ),
        }
        context = FetchContext(store_reader=lambda sid, s, e: store[sid])

        observations = DerivedProvider().fetch(
            series,
            start=date(2026, 5, 1),
            end=date(2026, 6, 30),
            session=cast(HttpSession, object()),
            context=context,
        )

        # The existing real-yield proxy keeps its canonical month-start grid.
        self.assertEqual(
            [(obs.observed_at, round(obs.value, 3)) for obs in observations],
            [(date(2026, 5, 1), 0.08), (date(2026, 6, 1), 0.02)],
        )

    def test_us_erp_monthly_alignment_uses_latest_input_date(self) -> None:
        series = _series("derived", "us.erp", unit="percent", frequency="monthly")
        store: dict[str, tuple[ObservationRecord, ...]] = {
            "us.sp500_earnings_yield": (
                observation("us.sp500_earnings_yield", date(2026, 5, 1), 5.0),
            ),
            "us.10y": (observation("us.10y", date(2026, 5, 29), 4.0),),
        }

        observations = DerivedProvider().fetch(
            series,
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            session=cast(HttpSession, object()),
            context=FetchContext(store_reader=lambda sid, s, e: store[sid]),
        )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 5, 29), 1.0)],
        )

    def test_derived_provider_monthly_alignment_skips_month_missing_an_input(self) -> None:
        series = _series("derived", "jp.real_10y_proxy", unit="percent", frequency="monthly")
        store: dict[str, tuple[ObservationRecord, ...]] = {
            "jp.10y": (
                observation("jp.10y", date(2026, 5, 29), 1.58),
                observation("jp.10y", date(2026, 6, 30), 1.62),  # June has a yield ...
            ),
            # ... but June core CPI is not released yet, so June must not emit a
            # half-computed proxy.
            "jp.cpi.core_yoy": (observation("jp.cpi.core_yoy", date(2026, 5, 1), 1.5),),
        }
        context = FetchContext(store_reader=lambda sid, s, e: store[sid])

        observations = DerivedProvider().fetch(
            series,
            start=date(2026, 5, 1),
            end=date(2026, 6, 30),
            session=cast(HttpSession, object()),
            context=context,
        )

        self.assertEqual([obs.observed_at for obs in observations], [date(2026, 5, 1)])

    def test_parse_cftc_json_computes_noncomm_net(self) -> None:
        series = _series("cftc", "097741", unit="contracts")
        text = json.dumps(
            [
                {
                    "report_date_as_yyyy_mm_dd": "2026-07-14T00:00:00.000",
                    "cftc_contract_market_code": "097741",
                    "noncomm_positions_long_all": "115965",
                    "noncomm_positions_short_all": "238628",
                }
            ]
        )

        observations = parse_cftc_json(series, text, start=date(2026, 7, 1), end=date(2026, 7, 31))

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 7, 14))
        self.assertEqual(observations[0].value, -122663.0)

    def test_parse_cftc_json_rejects_mismatched_contract_code(self) -> None:
        series = _series("cftc", "097741", unit="contracts")
        text = json.dumps(
            [
                {
                    "report_date_as_yyyy_mm_dd": "2026-07-14T00:00:00.000",
                    "cftc_contract_market_code": "999999",
                    "noncomm_positions_long_all": "1",
                    "noncomm_positions_short_all": "2",
                }
            ]
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "does not match requested"):
            parse_cftc_json(series, text, start=date(2026, 7, 1), end=date(2026, 7, 31))

    def test_parse_cftc_json_rejects_implausible_position(self) -> None:
        series = _series("cftc", "097741", unit="contracts")
        text = json.dumps(
            [
                {
                    "report_date_as_yyyy_mm_dd": "2026-07-14T00:00:00.000",
                    "cftc_contract_market_code": "097741",
                    "noncomm_positions_long_all": "9000000",
                    "noncomm_positions_short_all": "1",
                }
            ]
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "exceeds plausible"):
            parse_cftc_json(series, text, start=date(2026, 7, 1), end=date(2026, 7, 31))

    def test_parse_index_bars_extracts_close_and_filters_range(self) -> None:
        series = _series("jquants_indices", "topix", unit="index")
        rows = [
            {
                "Date": datetime(2026, 6, 30, tzinfo=UTC),
                "O": 3900.0,
                "H": 3950.0,
                "L": 3890.0,
                "C": 3940.5,
            },
            {
                "Date": datetime(2026, 7, 1, tzinfo=UTC),
                "O": 3945.0,
                "H": 4000.0,
                "L": 3940.0,
                "C": 3990.2,
            },
            {
                "Date": datetime(2026, 8, 1, tzinfo=UTC),
                "O": 4010.0,
                "H": 4020.0,
                "L": 4000.0,
                "C": 4015.0,
            },
        ]

        observations = parse_index_bars(series, rows, start=date(2026, 7, 1), end=date(2026, 7, 31))

        self.assertEqual(
            [(o.observed_at, o.value) for o in observations], [(date(2026, 7, 1), 3990.2)]
        )

    def test_parse_index_bars_rejects_implausible_close(self) -> None:
        series = _series("jquants_indices", "topix", unit="index")
        rows = [{"Date": datetime(2026, 7, 1, tzinfo=UTC), "C": 99999.0}]

        with self.assertRaisesRegex(IndicatorsProviderError, "outside plausible"):
            parse_index_bars(series, rows, start=date(2026, 7, 1), end=date(2026, 7, 31))

    def test_parse_index_bars_rejects_missing_close_column(self) -> None:
        series = _series("jquants_indices", "topix", unit="index")
        rows = [{"Date": datetime(2026, 7, 1, tzinfo=UTC), "O": 3945.0}]

        with self.assertRaisesRegex(IndicatorsProviderError, "missing a close column"):
            parse_index_bars(series, rows, start=date(2026, 7, 1), end=date(2026, 7, 31))

    def test_parse_nikkei_valuation_takes_weighted_average_and_filters_range(self) -> None:
        series = _series("nikkei_indexes", "per", unit="ratio")
        html = (
            "<table><tbody>"
            "<tr><!--daily_changing--><td>2026.06.30</td>"
            "<!--daily_changing--><td>18.40</td><!--daily_changing--><td>25.20</td></tr>"
            "<tr><!--daily_changing--><td>2026.07.24</td>"
            "<!--daily_changing--><td>17.82</td><!--daily_changing--><td>25.11</td></tr>"
            "</tbody></table>"
        )

        observations = parse_nikkei_valuation(
            series, html, start=date(2026, 7, 1), end=date(2026, 7, 31)
        )

        # The weighted-average column (17.82), not the index-based one (25.11).
        self.assertEqual(
            [(o.observed_at, o.value) for o in observations], [(date(2026, 7, 24), 17.82)]
        )

    def test_parse_nikkei_valuation_rejects_implausible_value(self) -> None:
        series = _series("nikkei_indexes", "per", unit="ratio")
        html = (
            "<tr><!--daily_changing--><td>2026.07.24</td>"
            "<!--daily_changing--><td>99.90</td><!--daily_changing--><td>120.0</td></tr>"
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "outside plausible"):
            parse_nikkei_valuation(series, html, start=date(2026, 7, 1), end=date(2026, 7, 31))

    def test_parse_boj_xlsx_refuses_a_workbook_it_cannot_read_the_named_column_from(
        self,
    ) -> None:
        # One damaged BOJ workbook per row: the series it was requested for, the
        # bytes, the window, and the refusal. Every row is a way the published
        # workbook has changed shape without the values leaving their band, which is
        # what makes reading the wrong column silent.
        refusals: tuple[tuple[str, SeriesDefinition, bytes, date, date, str], ...] = (
            (
                "shifted_value_column_even_when_value_is_in_band",
                _series(
                    "boj",
                    "3|Monetary Base|8|Unit: 100 million yen",
                    unit="jpy-100m",
                    plausible_min=2000.0,
                    plausible_max=100000000.0,
                ),
                _boj_workbook_bytes([(None, date(2026, 1, 31), 9999.0, 350000.0)], header_column=4),
                date(2026, 1, 1),
                date(2026, 1, 31),
                "header mismatch",
            ),
            (
                "empty_selected_column_for_in_range_dates",
                _series("boj", "3|Monetary Base|8|Unit: 100 million yen", unit="jpy-100m"),
                _boj_workbook_bytes(
                    [
                        (None, date(2026, 1, 31), None, 350000.0),
                        (None, date(2026, 2, 28), None, 360000.0),
                    ]
                ),
                date(2026, 1, 1),
                date(2026, 2, 28),
                "no numeric values",
            ),
            (
                "scale_metadata_change_with_in_band_value",
                _series(
                    "boj",
                    "3|Monetary Base|8|Unit: 100 million yen",
                    unit="jpy-100m",
                    plausible_min=2000.0,
                    plausible_max=100000000.0,
                ),
                _boj_workbook_bytes([(None, date(2026, 1, 31), 350000.0)], metadata="Unit: yen"),
                date(2026, 1, 1),
                date(2026, 1, 31),
                "metadata column 8 mismatch",
            ),
            (
                "non_xlsx_bytes",
                _series("boj", "3|Monetary Base|8|Unit: 100 million yen", unit="jpy-100m"),
                b"not a zip",
                date(2026, 1, 1),
                date(2026, 12, 31),
                "not a .xlsx",
            ),
            (
                "non_numeric_column_index",
                _series(
                    "boj", "BS01'MABJMTA|Monetary Base|8|Unit: 100 million yen", unit="jpy-100m"
                ),
                _boj_workbook_bytes([(None, date(2026, 1, 31), 1.0, 2.0)]),
                date(2026, 1, 1),
                date(2026, 12, 31),
                "start with a 1-based column index",
            ),
        )

        for name, series, content, start, end, reason in refusals:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(IndicatorsProviderError, reason),
            ):
                parse_boj_xlsx(series, content, start=start, end=end)

    def test_parse_boj_xlsx_extracts_value_column_and_filters_range(self) -> None:
        content = _boj_workbook_bytes(
            [
                (None, date(2026, 1, 31), 350000.0, 9999.0),
                (None, date(2026, 2, 28), 360000.0, 9999.0),
                (None, date(2026, 3, 31), 370000.0, 9999.0),
            ]
        )
        series = _series(
            "boj",
            "3|Monetary Base|8|Unit: 100 million yen",
            unit="jpy-100m",
        )

        observations = parse_boj_xlsx(
            series, content, start=date(2026, 2, 1), end=date(2026, 3, 31)
        )

        self.assertEqual(
            [(obs.observed_at, obs.value) for obs in observations],
            [(date(2026, 2, 1), 360000.0), (date(2026, 3, 1), 370000.0)],
        )

    def test_parse_boj_xlsx_skips_header_and_valueless_rows(self) -> None:
        content = _boj_workbook_bytes(
            [
                ("マネタリーベース", None, None, None),
                (None, date(2026, 1, 31), None, None),
                (None, date(2026, 2, 28), 360000.0, None),
            ]
        )
        series = _series(
            "boj",
            "3|Monetary Base|8|Unit: 100 million yen",
            unit="jpy-100m",
        )

        observations = parse_boj_xlsx(
            series, content, start=date(2026, 1, 1), end=date(2026, 12, 31)
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].value, 360000.0)

    def test_parse_boj_xlsx_does_not_accept_expected_metadata_from_an_adjacent_series(
        self,
    ) -> None:
        content = _boj_workbook_bytes(
            [(date(2026, 1, 1), 119.0, 100.0)],
            header_column=2,
            header_label="Real exports",
            metadata_column=2,
            metadata="s.a., CY 2025=100, 2025 prices",
            other_metadata=(3, "s.a., CY 2020=100, 2020 prices"),
        )
        series = _series(
            "boj",
            "2|Real exports|2|s.a., CY 2020=100, 2020 prices",
            unit="index",
            plausible_min=1.0,
            plausible_max=2000.0,
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "metadata column 2 mismatch"):
            parse_boj_xlsx(
                series,
                content,
                start=date(2026, 1, 1),
                end=date(2026, 1, 31),
            )

    def test_parse_boj_xlsx_wraps_corrupt_zip_as_provider_error(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("not-a-workbook.txt", "garbage")
        series = _series(
            "boj",
            "3|Monetary Base|8|Unit: 100 million yen",
            unit="jpy-100m",
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "could not be read"):
            parse_boj_xlsx(
                series, buffer.getvalue(), start=date(2026, 1, 1), end=date(2026, 12, 31)
            )

    def test_parse_mof_jgb_csv_extracts_10y_and_filters_range(self) -> None:
        series = _series("mof_jgb", "10年")
        text = "\n".join(
            [
                "国債金利情報,,,,,,,,,,,(単位 : %)",
                "基準日,1年,2年,3年,4年,5年,6年,7年,8年,9年,10年,15年",
                "R8.7.7,1.168,1.402,1.565,1.816,2.007,2.172,2.342,2.524,2.68,2.834,3.415",
                "R8.7.8,1.18,1.433,1.59,1.843,2.039,2.198,2.368,2.552,2.705,2.856,3.445",
                "R8.7.9,1.18,1.433,1.59,1.843,2.039,2.198,2.368,2.552,2.705,-,3.445",
            ]
        )

        observations = parse_mof_jgb_csv(
            series,
            text,
            start=date(2026, 7, 8),
            end=date(2026, 7, 9),
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 7, 8))
        self.assertEqual(observations[0].value, 2.856)

    def test_parse_mof_jgb_csv_rejects_missing_column(self) -> None:
        series = _series("mof_jgb", "10年")
        text = "基準日,1年\nR8.7.8,1.18\n"

        with self.assertRaisesRegex(IndicatorsProviderError, "missing column 10年"):
            parse_mof_jgb_csv(series, text, start=date(2026, 7, 8), end=date(2026, 7, 8))

    def test_parse_estat_json_filters_range_and_skips_nonnumeric(self) -> None:
        series = _series("estat", "0003427113", unit="index")
        text = _estat_payload(
            [
                {"@time": "2026000101", "$": "100.1"},
                {"@time": "2026000202", "$": "100.8"},
                {"@time": "2026000303", "$": "-"},
                {"@time": "2026000404", "$": "101.3"},
            ]
        )

        observations = parse_estat_json(series, text, start=date(2026, 2, 1), end=date(2026, 3, 31))

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 2, 1))
        self.assertEqual(observations[0].value, 100.8)

    def test_parse_estat_json_handles_single_value_object(self) -> None:
        series = _series("estat", "0003427113", unit="index")
        text = _estat_payload({"@time": "2026000505", "$": "102.0"})

        observations = parse_estat_json(
            series, text, start=date(2026, 1, 1), end=date(2026, 12, 31)
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 5, 1))
        self.assertEqual(observations[0].value, 102.0)

    def test_parse_estat_json_refuses_every_answer_it_cannot_read_one_series_from(
        self,
    ) -> None:
        # One malformed e-Stat answer per row: the series it was requested for, the
        # response body, the window, and the refusal it must produce. Every row is a shape
        # the API has actually returned; the parser has to keep refusing all of them.
        refusals: tuple[tuple[str, SeriesDefinition, str, date, date, str], ...] = (
            (
                "missing_structure",
                _series("estat", "0003427113", unit="index"),
                "{}",
                date(2026, 1, 1),
                date(2026, 12, 31),
                "GET_STATS_DATA",
            ),
            (
                # An e-Stat table carries dozens of series behind one statsDataId.
                #
                # A narrowing code the table does not define is answered by leaving that
                # dimension open, and the store's upsert would then keep whichever cell of
                # the period came last. The answer is checked cell by cell instead.
                "a_cell_outside_the_requested_narrowing",
                _series("estat", "0003355222?cdCat01=160&cdCat02=100&cdTab=100", unit="index"),
                _estat_payload(
                    [
                        {
                            "@tab": "100",
                            "@cat01": "160",
                            "@cat02": "100",
                            "@time": "2026000505",
                            "$": "961973.8",
                        },
                        {
                            "@tab": "100",
                            "@cat01": "110",
                            "@cat02": "100",
                            "@time": "2026000505",
                            "$": "2874019.3",
                        },
                    ]
                ),
                date(2026, 1, 1),
                date(2026, 12, 31),
                "outside the requested narrowing",
            ),
            (
                "a_narrowing_key_it_cannot_check",
                _series("estat", "0003355222?lvCat01=3", unit="index"),
                _estat_payload({"@time": "2026000505", "$": "102.0"}),
                date(2026, 1, 1),
                date(2026, 12, 31),
                "no row attribute",
            ),
            (
                "a_cell_spanning_more_than_one_month",
                _series("estat", "0003427113", unit="index"),
                _estat_payload({"@time": "2025000103", "$": "110.0"}),
                date(2025, 1, 1),
                date(2025, 12, 31),
                "more than one month",
            ),
            (
                "a_time_code_it_cannot_place",
                _series("estat", "0003427113", unit="index"),
                _estat_payload({"@time": "202501", "$": "110.0"}),
                date(2025, 1, 1),
                date(2025, 12, 31),
                "cannot place",
            ),
            (
                "a_rejected_request",
                _series("estat", "0003427113", unit="index"),
                json.dumps(
                    {
                        "GET_STATS_DATA": {
                            "RESULT": {"STATUS": 100, "ERROR_MSG": "統計表が存在しません。"}
                        }
                    }
                ),
                date(2026, 1, 1),
                date(2026, 12, 31),
                "status=100",
            ),
            (
                "one_page_of_a_longer_result",
                _series("estat", "0003427113", unit="index"),
                _estat_payload({"@time": "2026000505", "$": "102.0"}, next_key=100001),
                date(2026, 1, 1),
                date(2026, 12, 31),
                "one page of a longer result",
            ),
        )

        for name, series, text, start, end, reason in refusals:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(IndicatorsProviderError, reason),
            ):
                parse_estat_json(series, text, start=start, end=end)

    def test_parse_estat_json_reads_a_month_whose_closing_month_is_left_open(self) -> None:
        """e-Stat published 2024-01 of the watcher survey as "2024000100".

        Reading the closing field as the month drops that observation, and the
        series then has a hole no error ever reports.
        """

        series = _series("estat", "0003348423", unit="pt")
        text = _estat_payload({"@time": "2024000100", "$": "50.2"})

        observations = parse_estat_json(
            series, text, start=date(2024, 1, 1), end=date(2024, 12, 31)
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2024, 1, 1))
        self.assertEqual(observations[0].value, 50.2)

    def test_parse_estat_json_skips_the_year_totals_that_share_the_table(self) -> None:
        series = _series("estat", "0003427113", unit="index")
        text = _estat_payload(
            [
                {"@time": "2025100000", "$": "112.3"},
                {"@time": "2025000000", "$": "112.5"},
                {"@time": "2025000101", "$": "110.0"},
            ]
        )

        observations = parse_estat_json(
            series, text, start=date(2025, 1, 1), end=date(2025, 12, 31)
        )

        self.assertEqual(
            [(o.observed_at, o.value) for o in observations], [(date(2025, 1, 1), 110.0)]
        )

    def test_estat_dashboard_fetch_always_opens_the_request_at_the_history_floor(self) -> None:
        """ "No data" is the same answer as "unknown IndicatorCode" on this API.

        A request narrow enough to come back empty would make a mis-pinned series
        read as a quiet one, so every request asks from the floor, where a
        registered series always has observations. The stored window is still the
        requested one.
        """

        session = _RecordingSession(
            _dashboard_payload(
                [
                    _dashboard_row("20260400", "2.5"),
                    _dashboard_row("20260500", "2.4"),
                ]
            )
        )

        observations = EStatDashboardProvider().fetch(
            _dashboard_series(),
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            session=session,
        )

        self.assertEqual(session.params, {"TimeFrom": "19480100", "TimeTo": "20260500"})
        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 5, 1), 2.4)],
        )

    def test_parse_dashboard_json_skips_null_markers(self) -> None:
        observations = _parse_dashboard(
            [
                _dashboard_row("20260300", "2.6"),
                _dashboard_row("20260400", "-"),
                _dashboard_row("20260500", "2.4"),
            ]
        )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 3, 1), 2.6), (date(2026, 5, 1), 2.4)],
        )

    def test_parse_dashboard_json_rejects_a_row_the_selectors_excluded(self) -> None:
        """One IndicatorCode answers several cycles and adjustments at once.

        A row from another one means the filter did not apply, and mixing a
        month-on-month change into an index level would read as the series itself.
        """

        for attribute, value in (
            ("@cycle", "3"),
            ("@isSeasonal", "1"),
            ("@indicator", "0302030202010090010"),
            ("@regionCode", "13000"),
        ):
            with self.subTest(attribute=attribute):
                row = _dashboard_row("20260500", "2.4")
                row["VALUE"][attribute] = value

                with self.assertRaisesRegex(IndicatorsProviderError, attribute):
                    _parse_dashboard([row])

    def test_parse_dashboard_json_leaves_a_preliminary_month_unwritten(self) -> None:
        # The declared publication lag belongs to the final print, so a month that
        # has only a preliminary print is not due yet rather than missing.
        preliminary = _dashboard_row("20260600", "2.9")
        preliminary["VALUE"]["@isProvisional"] = "1"

        with self.assertLogs(
            "baibai_engine.macro.indicators.providers.estat_dashboard", level="WARNING"
        ) as logs:
            observations = _parse_dashboard(
                [
                    _dashboard_row("20260400", "2.6"),
                    _dashboard_row("20260500", "2.4"),
                    preliminary,
                ]
            )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 4, 1), 2.6), (date(2026, 5, 1), 2.4)],
        )
        message = "\n".join(logs.output)
        self.assertIn("test.series", message)
        self.assertIn("2026-06-01", message)

    def test_parse_dashboard_json_keeps_a_preliminary_month_out_of_the_unit_check(self) -> None:
        # A preliminary row is not part of the series being written, so its unit
        # must not read as the publisher mixing two units into one series.
        preliminary = _dashboard_row("20260600", "2.9")
        preliminary["VALUE"]["@isProvisional"] = "1"
        preliminary["VALUE"]["@unit"] = "指数"

        observations = _parse_dashboard([_dashboard_row("20260500", "2.4"), preliminary])

        self.assertEqual([item.observed_at for item in observations], [date(2026, 5, 1)])

    def test_parse_dashboard_json_rejects_a_page_shorter_than_its_declared_total(self) -> None:
        text = _dashboard_payload([_dashboard_row("20260500", "2.4")], total=2)

        with self.assertRaisesRegex(IndicatorsProviderError, "1 rows for a declared total of 2"):
            parse_dashboard_json(
                _dashboard_series(),
                text,
                start=date(2026, 1, 1),
                end=date(2026, 12, 31),
                selectors=_DASHBOARD_SELECTORS,
            )

    def test_parse_dashboard_json_rejects_an_api_error_status(self) -> None:
        text = json.dumps(
            {"GET_STATS": {"RESULT": {"status": "1", "errorMsg": "該当データはありませんでした。"}}}
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "returned status 1"):
            parse_dashboard_json(
                _dashboard_series(),
                text,
                start=date(2026, 1, 1),
                end=date(2026, 12, 31),
                selectors=_DASHBOARD_SELECTORS,
            )

    def test_parse_dashboard_json_rejects_an_unreadable_time_code(self) -> None:
        # A changed time axis must fail rather than pass as a shorter history.
        for time_code in ("2026Q100", "202605", "20261300"):
            with (
                self.subTest(time_code=time_code),
                self.assertRaisesRegex(IndicatorsProviderError, "time code is unreadable"),
            ):
                _parse_dashboard([_dashboard_row(time_code, "2.4")])

    def test_parse_dashboard_json_rejects_a_value_that_is_not_a_string(self) -> None:
        # Missing numbers have their own markers, so a changed value type is a
        # changed response shape and must not read as a row that has no value.
        row = _dashboard_row("20260500", "2.4")
        row["VALUE"]["$"] = cast(str, 2.4)

        with self.assertRaisesRegex(IndicatorsProviderError, "value is not a string"):
            _parse_dashboard([row])

    def test_parse_dashboard_json_rejects_two_units_in_one_series(self) -> None:
        rows = [_dashboard_row("20260400", "2.5"), _dashboard_row("20260500", "2400")]
        rows[1]["VALUE"]["@unit"] = "120"

        with self.assertRaisesRegex(IndicatorsProviderError, "mixed units"):
            _parse_dashboard(rows)

    def test_estat_dashboard_requires_the_source_url_to_pin_one_series(self) -> None:
        """The source URL is the provenance stored on every observation.

        Keeping the identity anywhere else would let the recorded provenance and
        the fetched series drift apart, and a corrected selector would then layer a
        second statistic onto the same series instead of replacing it.
        """

        base = "https://dashboard.e-stat.go.jp/api/1.0/Json/getData"
        for source_url, message in (
            (base, "missing selector"),
            (f"{base}?IndicatorCode={_DASHBOARD_INDICATOR}&Cycle=1", "missing selector"),
            (f"{_DASHBOARD_SOURCE_URL}&Unit=001", "unsupported"),
            (f"{_DASHBOARD_SOURCE_URL}&Cycle=1", "repeated"),
            (
                (
                    f"{base}?Lang=JP&IndicatorCode={_DASHBOARD_INDICATOR}"
                    "&Cycle=1&IsSeasonalAdjustment=2&RegionCode="
                ),
                "empty",
            ),
            (
                (
                    f"{base}?Lang=JP&IndicatorCode={_DASHBOARD_INDICATOR}"
                    "&Cycle=3&IsSeasonalAdjustment=2&RegionCode=00000"
                ),
                "monthly cycle only",
            ),
            (
                (
                    f"{base}?Lang=JP&IndicatorCode=0302030202010090010"
                    "&Cycle=1&IsSeasonalAdjustment=2&RegionCode=00000"
                ),
                "for series",
            ),
        ):
            with (
                self.subTest(source_url=source_url),
                self.assertRaisesRegex(IndicatorsProviderError, message),
            ):
                EStatDashboardProvider().fetch(
                    _dashboard_series(source_url=source_url),
                    start=date(2026, 1, 1),
                    end=date(2026, 5, 31),
                    session=_RecordingSession(_dashboard_payload([])),
                )

    def test_estat_dashboard_refuses_a_series_that_is_not_monthly(self) -> None:
        with self.assertRaisesRegex(IndicatorsProviderError, "monthly series only"):
            EStatDashboardProvider().fetch(
                _dashboard_series(frequency="quarterly"),
                start=date(2026, 1, 1),
                end=date(2026, 5, 31),
                session=_RecordingSession(_dashboard_payload([])),
            )

    def test_parse_yahoo_chart_filters_range_and_skips_null_close(self) -> None:
        series = _series("yahoo", "GC=F", unit="usd-per-oz")

        def ts(year: int, month: int, day: int) -> int:
            return int(datetime(year, month, day, 14, 30, tzinfo=UTC).timestamp())

        text = json.dumps(
            {
                "chart": {
                    "error": None,
                    "result": [
                        {
                            "timestamp": [ts(2026, 5, 1), ts(2026, 5, 4), ts(2026, 5, 5)],
                            "indicators": {"quote": [{"close": [4000.0, None, 4050.5]}]},
                        }
                    ],
                }
            }
        )

        observations = parse_yahoo_chart(series, text, start=date(2026, 5, 4), end=date(2026, 5, 5))

        self.assertEqual([obs.observed_at for obs in observations], [date(2026, 5, 5)])
        self.assertEqual(observations[0].value, 4050.5)

    def test_parse_yahoo_chart_rejects_error_payload(self) -> None:
        series = _series("yahoo", "BADSYM", unit="index")
        text = json.dumps(
            {"chart": {"result": None, "error": {"code": "Not Found", "description": "no data"}}}
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "Yahoo chart error"):
            parse_yahoo_chart(series, text, start=date(2026, 5, 1), end=date(2026, 5, 1))

    def test_parse_yahoo_chart_rejects_missing_quote(self) -> None:
        series = _series("yahoo", "GC=F", unit="usd-per-oz")
        text = json.dumps(
            {"chart": {"error": None, "result": [{"timestamp": [1], "indicators": {}}]}}
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "missing quote"):
            parse_yahoo_chart(series, text, start=date(2026, 5, 1), end=date(2026, 5, 2))

    def test_parse_multpl_current_extracts_value(self) -> None:
        html = (
            '<meta name="description" content="Current Shiller PE Ratio is 40.70, '
            'a change of -0.30 from previous market close." />'
        )
        self.assertEqual(parse_multpl_current(html, "shiller-pe"), 40.70)

    def test_parse_multpl_current_reads_percent_value(self) -> None:
        text = "Current S&P 500 Earnings Yield is 3.18%, a change of +2.36 bps."
        self.assertEqual(parse_multpl_current(text, "s-p-500-earnings-yield"), 3.18)

    def test_parse_multpl_current_rejects_unparseable(self) -> None:
        with self.assertRaisesRegex(IndicatorsProviderError, "cannot parse current value"):
            parse_multpl_current("<html>no current sentence here</html>", "shiller-pe")

    def test_parse_multpl_history_extracts_monthly_rows_and_current_level(self) -> None:
        series = _series("multpl", "s-p-500-earnings-yield", unit="percent")
        html = """
        <table id="datatable">
          <tr><th>Date</th><th>Value</th></tr>
          <tr><td>Jul 22, 2026</td><td>3.18%</td></tr>
          <tr><td>Jul 1, 2026</td><td>† 3.20%</td></tr>
          <tr><td>Jun 1, 2026</td><td>3.15%</td></tr>
        </table>
        """

        observations = parse_multpl_history(
            series,
            html,
            start=date(2026, 7, 1),
            end=date(2026, 7, 22),
        )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 7, 22), 3.18), (date(2026, 7, 1), 3.20)],
        )

    def test_parse_multpl_history_rejects_missing_table(self) -> None:
        series = _series("multpl", "shiller-pe")

        with self.assertRaisesRegex(IndicatorsProviderError, "historical table missing"):
            parse_multpl_history(
                series,
                "<html><table></table></html>",
                start=date(1871, 1, 1),
                end=date(2026, 7, 22),
            )

    def test_multpl_long_range_uses_history_table(self) -> None:
        series = _series("multpl", "shiller-pe")
        response = _FakeResponse(
            b'<table id="datatable"><tr><td>Jul 1, 2026</td><td>37.50</td></tr></table>'
        )

        with patch(
            "baibai_engine.macro.indicators.providers.multpl._today_jst",
            return_value=date(2026, 7, 20),
        ):
            observations = fetch_observations(
                series,
                start=date(2024, 1, 1),
                end=date(2026, 7, 20),
                session=_StaticSession(response),
            )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 7, 1), 37.5)],
        )

    def test_multpl_all_history_rejects_missing_floor(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = '<table id="datatable"><tr><td>Jul 1, 2026</td><td>40.0</td></tr></table>'

        with self.assertRaisesRegex(IndicatorsProviderError, "must start at 1871-02-01"):
            parse_multpl_history(
                series,
                html,
                start=date(1871, 1, 1),
                end=date(2026, 7, 20),
            )

    def test_parse_multpl_history_accepts_month_start_without_current_month_row(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = """
        <table id="datatable">
          <tr><th>Date</th><th>Value</th></tr>
          <tr><td>Aug 3, 2026</td><td>37.80</td></tr>
          <tr><td>Jul 1, 2026</td><td>37.50</td></tr>
          <tr><td>Jun 1, 2026</td><td>37.00</td></tr>
        </table>
        """

        with patch(
            "baibai_engine.macro.indicators.providers.multpl._today_jst",
            return_value=date(2026, 8, 3),
        ):
            observations = parse_multpl_history(
                series,
                html,
                start=date(2026, 7, 20),
                end=date(2026, 8, 3),
            )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 8, 3), 37.80)],
        )

    def test_parse_multpl_history_accepts_prior_month_current_row_after_rollover(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = """
        <table id="datatable">
          <tr><th>Date</th><th>Value</th></tr>
          <tr><td>Aug 31, 2026</td><td>42.04</td></tr>
          <tr><td>Jul 1, 2026</td><td>40.73</td></tr>
          <tr><td>Jun 1, 2026</td><td>40.50</td></tr>
        </table>
        """

        with patch(
            "baibai_engine.macro.indicators.providers.multpl._today_jst",
            return_value=date(2026, 9, 1),
        ):
            observations = parse_multpl_history(
                series,
                html,
                start=date(2026, 8, 18),
                end=date(2026, 9, 1),
            )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 8, 31), 42.04)],
        )

    def test_parse_multpl_history_accepts_delayed_prior_month_asof(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = """
        <table id="datatable">
          <tr><th>Date</th><th>Value</th></tr>
          <tr><td>Aug 31, 2026</td><td>42.04</td></tr>
          <tr><td>Jul 1, 2026</td><td>40.73</td></tr>
          <tr><td>Jun 1, 2026</td><td>40.50</td></tr>
        </table>
        """

        with patch(
            "baibai_engine.macro.indicators.providers.multpl._today_jst",
            return_value=date(2026, 9, 1),
        ):
            observations = parse_multpl_history(
                series,
                html,
                start=date(2026, 8, 17),
                end=date(2026, 8, 31),
            )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 8, 31), 42.04)],
        )

    def test_parse_multpl_history_rejects_history_ending_before_previous_month(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = """
        <table id="datatable">
          <tr><th>Date</th><th>Value</th></tr>
          <tr><td>Aug 3, 2026</td><td>37.80</td></tr>
          <tr><td>Jun 1, 2026</td><td>37.00</td></tr>
          <tr><td>May 1, 2026</td><td>36.50</td></tr>
        </table>
        """

        with (
            patch(
                "baibai_engine.macro.indicators.providers.multpl._today_jst",
                return_value=date(2026, 8, 3),
            ),
            self.assertRaisesRegex(IndicatorsProviderError, "ends before 2026-07-01"),
        ):
            parse_multpl_history(
                series,
                html,
                start=date(2026, 7, 20),
                end=date(2026, 8, 3),
            )

    def test_multpl_all_history_accepts_month_start_without_current_month_row(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = _multpl_monthly_table(floor=date(1871, 2, 1), latest=date(2026, 7, 1))

        with patch(
            "baibai_engine.macro.indicators.providers.multpl._today_jst",
            return_value=date(2026, 8, 3),
        ):
            observations = parse_multpl_history(
                series,
                html,
                start=date(1871, 1, 1),
                end=date(2026, 8, 3),
            )

        self.assertEqual(observations[0].observed_at, date(2026, 7, 1))
        self.assertEqual(observations[-1].observed_at, date(1871, 2, 1))

    def test_multpl_all_history_rejects_gap_before_current_month(self) -> None:
        series = _series("multpl", "shiller-pe")
        html = _multpl_monthly_table(
            floor=date(1871, 2, 1), latest=date(2026, 7, 1), omit=date(2020, 5, 1)
        )

        with (
            patch(
                "baibai_engine.macro.indicators.providers.multpl._today_jst",
                return_value=date(2026, 8, 3),
            ),
            self.assertRaisesRegex(IndicatorsProviderError, "missing monthly value.*2020-05-01"),
        ):
            parse_multpl_history(
                series,
                html,
                start=date(1871, 1, 1),
                end=date(2026, 8, 3),
            )

    def test_multpl_uses_japan_operation_date(self) -> None:
        series = _series("multpl", "shiller-pe")
        response = _FakeResponse(b"Current Shiller PE Ratio is 40.70")

        with patch(
            "baibai_engine.macro.indicators.providers.multpl._today_jst",
            return_value=date(2026, 7, 20),
        ):
            observations = fetch_observations(
                series,
                start=date(2026, 7, 20),
                end=date(2026, 7, 20),
                session=_StaticSession(response),
            )

        self.assertEqual([item.observed_at for item in observations], [date(2026, 7, 20)])

    def test_parse_tsr_bankruptcies_json_reads_current_and_legacy_entries(self) -> None:
        series = _series("tsr_bankruptcies", "jp_bankruptcies_tsr", unit="count")
        text = json.dumps(
            [
                {
                    "period_division": "月次",
                    "title": "2026年6月の全国企業倒産1,021件",
                    "free_word": [],
                },
                {
                    "period_division": "月次",
                    "title": "2026年（令和8年） 5月度 全国企業倒産状況",
                    "free_word": [
                        "title",
                        "<table><tr><th>倒産件数</th><td>993 件</td></tr></table>",
                    ],
                },
                {
                    "period_division": "年間",
                    "title": "2025年の全国企業倒産10,000件",
                },
            ]
        )

        observations = parse_tsr_bankruptcies_json(
            series,
            text,
            start=date(2026, 5, 1),
            end=date(2026, 6, 1),
        )

        self.assertEqual(
            [(item.observed_at, item.value) for item in observations],
            [(date(2026, 5, 1), 993.0), (date(2026, 6, 1), 1021.0)],
        )

    def test_parse_tsr_bankruptcies_json_refuses_a_listing_it_cannot_read_a_history_from(
        self,
    ) -> None:
        # One damaged TSR listing per row. The provider reads a monthly count out of
        # press-release titles, so a title it cannot parse, a month it never saw, and a
        # history that does not reach the floor are all silent holes unless refused.
        series = _series("tsr_bankruptcies", "jp_bankruptcies_tsr", unit="count")
        refusals: tuple[tuple[str, str, date, date, str], ...] = (
            (
                "unparseable_monthly_entry",
                json.dumps(
                    [
                        {
                            "period_division": "月次",
                            "title": "月次の全国企業倒産状況",
                            "free_word": [],
                        }
                    ]
                ),
                date(2003, 1, 1),
                date(2026, 6, 1),
                "cannot parse monthly entry",
            ),
            (
                "history_gap",
                json.dumps(
                    [
                        {"period_division": "月次", "title": "2026年4月の全国企業倒産990件"},
                        {"period_division": "月次", "title": "2026年6月の全国企業倒産1,021件"},
                    ]
                ),
                date(2026, 4, 1),
                date(2026, 6, 1),
                "missing monthly entries",
            ),
            (
                "missing_history_floor",
                json.dumps(
                    [{"period_division": "月次", "title": "2026年6月の全国企業倒産1,021件"}]
                ),
                date(2003, 1, 1),
                date(2026, 7, 20),
                "must start at 2003-01-01",
            ),
        )

        for name, text, start, end, reason in refusals:
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(IndicatorsProviderError, reason),
            ):
                parse_tsr_bankruptcies_json(series, text, start=start, end=end)

    def test_parse_tsr_bankruptcies_json_rejects_missing_latest_release(self) -> None:
        series = _series("tsr_bankruptcies", "jp_bankruptcies_tsr", unit="count")
        rows = []
        current = date(2003, 1, 1)
        while current <= date(2026, 5, 1):
            rows.append(
                {
                    "period_division": "月次",
                    "title": f"{current.year}年{current.month}月の全国企業倒産1,000件",
                }
            )
            current = (
                date(current.year + 1, 1, 1)
                if current.month == 12
                else date(current.year, current.month + 1, 1)
            )

        with (
            patch(
                "baibai_engine.macro.indicators.providers.tsr_bankruptcies._today_jst",
                return_value=date(2026, 7, 28),
            ),
            self.assertRaisesRegex(IndicatorsProviderError, "latest expected release"),
        ):
            parse_tsr_bankruptcies_json(
                series,
                json.dumps(rows),
                start=date(2003, 1, 1),
                end=date(2026, 7, 28),
            )

    def test_parse_tsr_bankruptcies_json_ignores_future_end_for_latest_release(
        self,
    ) -> None:
        series = _series("tsr_bankruptcies", "jp_bankruptcies_tsr", unit="count")
        rows = []
        current = date(2003, 1, 1)
        while current <= date(2026, 6, 1):
            rows.append(
                {
                    "period_division": "月次",
                    "title": f"{current.year}年{current.month}月の全国企業倒産1,000件",
                }
            )
            current = (
                date(current.year + 1, 1, 1)
                if current.month == 12
                else date(current.year, current.month + 1, 1)
            )

        with patch(
            "baibai_engine.macro.indicators.providers.tsr_bankruptcies._today_jst",
            return_value=date(2026, 7, 24),
        ):
            observations = parse_tsr_bankruptcies_json(
                series,
                json.dumps(rows),
                start=date(2003, 1, 1),
                end=date(2026, 12, 31),
            )

        self.assertEqual(len(observations), 282)
        self.assertEqual(observations[-1].observed_at, date(2026, 6, 1))

    def test_parse_tsr_bankruptcies_json_allows_prepublication_tail(self) -> None:
        series = _series("tsr_bankruptcies", "jp_bankruptcies_tsr", unit="count")
        rows = []
        current = date(2003, 1, 1)
        while current <= date(2026, 5, 1):
            rows.append(
                {
                    "period_division": "月次",
                    "title": f"{current.year}年{current.month}月の全国企業倒産1,000件",
                }
            )
            current = (
                date(current.year + 1, 1, 1)
                if current.month == 12
                else date(current.year, current.month + 1, 1)
            )

        with patch(
            "baibai_engine.macro.indicators.providers.tsr_bankruptcies._today_jst",
            return_value=date(2026, 7, 5),
        ):
            observations = parse_tsr_bankruptcies_json(
                series,
                json.dumps(rows),
                start=date(2003, 1, 1),
                end=date(2026, 7, 5),
            )

        self.assertEqual(observations[-1].observed_at, date(2026, 5, 1))

    def test_parse_boj_timeseries_json_filters_range_and_nulls(self) -> None:
        series = _series("boj_timeseries", "FM01:STRDCLUCON", unit="percent")
        text = json.dumps(
            {
                "STATUS": 200,
                "RESULTSET": [
                    {
                        "SERIES_CODE": "STRDCLUCON",
                        "VALUES": {
                            "SURVEY_DATES": [19980104, 19980105, 19980106],
                            "VALUES": [None, 0.49, 0.42],
                        },
                    }
                ],
            }
        )

        observations = parse_boj_timeseries_json(
            series,
            text,
            start=date(1998, 1, 5),
            end=date(1998, 1, 5),
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(1998, 1, 5))
        self.assertEqual(observations[0].value, 0.49)

    def test_parse_boj_timeseries_json_reads_monthly_yyyymm_survey_dates(self) -> None:
        series = _series(
            "boj_timeseries", "PR01:PRCG20_2200000000", unit="index", frequency="monthly"
        )
        text = json.dumps(
            {
                "STATUS": 200,
                "RESULTSET": [
                    {
                        "SERIES_CODE": "PRCG20_2200000000",
                        "VALUES": {
                            "SURVEY_DATES": [202605, 202606, 202607],
                            "VALUES": [134.9, 135.4, None],
                        },
                    }
                ],
            }
        )

        observations = parse_boj_timeseries_json(
            series, text, start=date(2026, 5, 1), end=date(2026, 7, 31)
        )

        self.assertEqual(
            [(o.observed_at, o.value) for o in observations],
            [(date(2026, 5, 1), 134.9), (date(2026, 6, 1), 135.4)],
        )

    def test_parse_boj_timeseries_json_reads_quarterly_yyyy0q_survey_dates(self) -> None:
        series = _series(
            "boj_timeseries", "CO:TK99F1000601GCQ01000", unit="pt", frequency="quarterly"
        )
        text = json.dumps(
            {
                "STATUS": 200,
                "RESULTSET": [
                    {
                        "SERIES_CODE": "TK99F1000601GCQ01000",
                        "VALUES": {
                            "SURVEY_DATES": [202504, 202601, 202602],
                            "VALUES": [15, 17, 22],
                        },
                    }
                ],
            }
        )

        observations = parse_boj_timeseries_json(
            series, text, start=date(2025, 1, 1), end=date(2026, 12, 31)
        )

        # YYYY0Q maps Q1..Q4 to the last month of the quarter (Mar/Jun/Sep/Dec).
        self.assertEqual(
            [(o.observed_at, o.value) for o in observations],
            [(date(2025, 12, 1), 15.0), (date(2026, 3, 1), 17.0), (date(2026, 6, 1), 22.0)],
        )

    def test_parse_boj_timeseries_json_rejects_wrong_length_for_frequency(self) -> None:
        series = _series(
            "boj_timeseries", "PR01:PRCG20_2200000000", unit="index", frequency="monthly"
        )
        text = json.dumps(
            {
                "STATUS": 200,
                "RESULTSET": [
                    {
                        "SERIES_CODE": "PRCG20_2200000000",
                        "VALUES": {"SURVEY_DATES": [20260601], "VALUES": [135.4]},
                    }
                ],
            }
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "invalid survey date"):
            parse_boj_timeseries_json(series, text, start=date(2026, 1, 1), end=date(2026, 12, 31))

    def test_parse_boj_timeseries_json_rejects_malformed_payload(self) -> None:
        series = _series("boj_timeseries", "FM01:STRDCLUCON", unit="percent")
        for payload, message in (
            ({"STATUS": 500, "MESSAGE": "failed"}, "status"),
            (
                {
                    "STATUS": 200,
                    "RESULTSET": [
                        {
                            "SERIES_CODE": "STRDCLUCON",
                            "VALUES": {"SURVEY_DATES": [19980105], "VALUES": []},
                        }
                    ],
                },
                "lengths differ",
            ),
        ):
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(IndicatorsProviderError, message),
            ):
                parse_boj_timeseries_json(
                    series,
                    json.dumps(payload),
                    start=date(1998, 1, 1),
                    end=date(1998, 1, 31),
                )

    def test_split_stats_data_id_extracts_narrowing_params(self) -> None:
        from baibai_engine.macro.indicators.providers.estat import _split_stats_data_id

        stats_id, narrowing = _split_stats_data_id("0003427113?cdCat01=0001&cdArea=00000&cdTab=1")

        self.assertEqual(stats_id, "0003427113")
        self.assertEqual(narrowing, {"cdCat01": "0001", "cdArea": "00000", "cdTab": "1"})

    def test_split_stats_data_id_without_query_returns_empty_params(self) -> None:
        from baibai_engine.macro.indicators.providers.estat import _split_stats_data_id

        self.assertEqual(_split_stats_data_id("0003427113"), ("0003427113", {}))

    def test_parse_trades_spec_filters_range_and_uses_foreign_balance(self) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy-thousand")
        rows = [
            {
                "PubDate": "2026-05-08",
                "EnDate": "2026-04-25",
                "FrgnBuy": 900,
                "FrgnSell": 800,
                "FrgnBal": 100,
            },
            {
                "PubDate": "2026-05-08",
                "EnDate": "2026-05-02",
                "FrgnBuy": 1500,
                "FrgnSell": 1000,
                "FrgnBal": 500,
            },
            {
                "PubDate": "2026-05-15",
                "EnDate": "2026-05-09",
                "FrgnBuy": 1200,
                "FrgnSell": 2000,
                "FrgnBal": -800,
            },
            {
                "PubDate": "2026-05-22",
                "EnDate": "2026-05-16",
                "FrgnBuy": 300,
                "FrgnSell": 100,
                "FrgnBal": 200,
            },
        ]

        observations = parse_trades_spec(
            series, rows, start=date(2026, 5, 1), end=date(2026, 5, 15)
        )

        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0].observed_at, date(2026, 5, 2))
        self.assertEqual(observations[0].value, 500.0)
        self.assertEqual(observations[0].vintage_at, datetime(2026, 5, 8, tzinfo=UTC))
        self.assertEqual(observations[1].observed_at, date(2026, 5, 9))
        self.assertEqual(observations[1].value, -800.0)
        self.assertEqual(observations[0].unit, "jpy-thousand")

    def test_parse_trades_spec_falls_back_to_purchases_minus_sales(self) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy-thousand")
        rows = [
            {"PubDate": "2026-05-08", "EnDate": "2026-05-08", "FrgnBuy": 1500, "FrgnSell": 1000}
        ]

        observations = parse_trades_spec(series, rows, start=date(2026, 5, 8), end=date(2026, 5, 8))

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].value, 500.0)

    def test_parse_trades_spec_keeps_each_period_for_duplicate_publication_date(
        self,
    ) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy-thousand")
        rows = [
            {
                "PubDate": "2024-09-10",
                "StDate": "2024-08-19",
                "EnDate": "2024-08-23",
                "FrgnBal": -408854431,
            },
            {
                "PubDate": "2024-09-10",
                "StDate": "2024-08-26",
                "EnDate": "2024-08-30",
                "FrgnBal": -237009201,
            },
        ]

        observations = parse_trades_spec(
            series,
            rows,
            start=date(2024, 8, 1),
            end=date(2024, 9, 30),
        )

        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0].observed_at, date(2024, 8, 23))
        self.assertEqual(observations[0].period_start, date(2024, 8, 19))
        self.assertEqual(observations[0].period_end, date(2024, 8, 23))
        self.assertEqual(observations[0].value, -408854431.0)
        self.assertEqual(observations[1].observed_at, date(2024, 8, 30))
        self.assertEqual(observations[1].period_start, date(2024, 8, 26))
        self.assertEqual(observations[1].period_end, date(2024, 8, 30))
        self.assertEqual(observations[1].value, -237009201.0)
        self.assertEqual(observations[0].vintage_at, datetime(2024, 9, 10, tzinfo=UTC))
        self.assertEqual(observations[1].vintage_at, datetime(2024, 9, 10, tzinfo=UTC))

    def test_parse_trades_spec_rejects_missing_foreign_columns(self) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy-thousand")
        rows = [{"PubDate": "2026-05-08", "EnDate": "2026-05-08", "Section": "TSEPrime"}]

        with self.assertRaisesRegex(IndicatorsProviderError, "missing"):
            parse_trades_spec(series, rows, start=date(2026, 5, 8), end=date(2026, 5, 8))

    def test_parse_trades_spec_rejects_a_row_without_an_aggregation_period_end(self) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy-thousand")
        rows = [{"PubDate": "2026-05-08", "FrgnBal": 500}]

        with self.assertRaisesRegex(IndicatorsProviderError, "aggregation period end"):
            parse_trades_spec(series, rows, start=date(2026, 5, 1), end=date(2026, 5, 15))

    def test_parse_trades_spec_dates_the_week_by_its_end_not_its_publication(self) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy-thousand")
        rows = [
            {
                "PubDate": "2026-07-02",
                "StDate": "2026-06-22",
                "EnDate": "2026-06-26",
                "FrgnBal": -1208567543,
            }
        ]

        observations = parse_trades_spec(
            series, rows, start=date(2026, 6, 1), end=date(2026, 7, 31)
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 6, 26))
        self.assertEqual(observations[0].period_start, date(2026, 6, 22))
        self.assertEqual(observations[0].vintage_at, datetime(2026, 7, 2, tzinfo=UTC))

    def test_fetch_observations_rejects_unknown_provider(self) -> None:
        series = _series("nonexistent_provider", "X")

        with self.assertRaisesRegex(
            IndicatorsProviderError, "unsupported indicator provider: nonexistent_provider"
        ):
            fetch_observations(series, start=date(2026, 5, 1), end=date(2026, 5, 1))


class IndicatorsRegistryTests(unittest.TestCase):
    def test_point_in_time_read_contracts_match_registered_provider_specs(self) -> None:
        self.assertEqual(
            point_in_time_providers(),
            frozenset(spec.name for spec in registered_specs() if spec.point_in_time_vintage),
        )

    def test_canonical_registry_membership_has_a_known_generation(self) -> None:
        self.assertEqual(load_definitions().generation, 5)
        with (
            patch(
                "baibai_engine.macro.indicators.definitions._REGISTRY_MEMBERSHIP_GENERATIONS",
                {},
            ),
            self.assertRaisesRegex(ValueError, "without a new generation digest"),
        ):
            load_definitions()

    def test_new_tier1_series_registered_with_expected_provider_and_category(self) -> None:
        by_id = load_definitions().by_id()
        expected = {
            "jp.nikkei225": ("fred_csv", "equity-index"),
            "jp.policy_rate": ("boj_timeseries", "policy"),
            "jp.10y": ("mof_jgb", "rates"),
            "jp.unemployment": ("estat_dashboard", "labor"),
            "jp.nominal_wage_index": ("estat_dashboard", "labor"),
            "jp.real_effective_exchange_rate": ("fred_csv", "fx"),
            "jp.cpi.services": ("estat", "inflation"),
            "credit.us_hy_oas": ("fred_csv", "credit"),
            "credit.us_ccc_oas": ("fred_csv", "credit"),
            "btc_usd": ("fred_csv", "crypto"),
            "jp.machinery_orders": ("estat", "activity"),
            "jp.watcher_current_di": ("estat", "activity"),
            "jp.consumer_confidence": ("estat", "activity"),
            "jp.bank_lending_yoy": ("boj_timeseries", "monetary"),
            "jp.tankan_large_nonmfg_di": ("boj_timeseries", "activity"),
            "jp.real_wage_index": ("estat_dashboard", "labor"),
            "us.empire_manufacturing": ("fred_csv", "activity"),
            "us.philly_fed_manufacturing": ("fred_csv", "activity"),
            "jp.n225_iv_30d": ("jquants_options", "volatility"),
            "jp.n225_iv_skew": ("jquants_options", "volatility"),
            "jp.n225_iv_term": ("jquants_options", "volatility"),
        }

        for series_id, (provider, category) in expected.items():
            self.assertIn(series_id, by_id)
            self.assertEqual(by_id[series_id].provider, provider)
            self.assertEqual(by_id[series_id].category, category)

        self.assertEqual(
            by_id["jp.nominal_wage_index"].provider_series_id,
            "0302030202010090010",
        )
        self.assertEqual(
            by_id["jp.unemployment"].provider_series_id,
            "0301010000020020010",
        )

        self.assertEqual(
            by_id["jp.real_effective_exchange_rate"].provider_series_id,
            "RBJPBIS",
        )
        self.assertEqual(
            by_id["jp.cpi.services"].provider_series_id,
            "0003427113?cdCat01=0220&cdArea=00000&cdTab=1",
        )
        # The narrowing codes are the series identity for an e-Stat table that
        # carries dozens of series, so an edit to them changes what is stored
        # without changing anything else the tests look at.
        self.assertEqual(
            by_id["jp.machinery_orders"].provider_series_id,
            "0003355222?cdCat01=160&cdCat02=100&cdTab=100",
        )
        self.assertEqual(
            by_id["jp.watcher_current_di"].provider_series_id,
            "0003348423?cdCat01=100&cdCat02=100&cdTab=140",
        )
        self.assertEqual(
            by_id["jp.consumer_confidence"].provider_series_id,
            "0003446462?cdCat01=1060&cdTab=200",
        )
        self.assertEqual(
            by_id["jp.bank_lending_yoy"].provider_series_id,
            "MD13:FAAPOBAL1@",
        )

    def test_estat_series_narrow_on_dimensions_the_answer_can_be_checked_against(self) -> None:
        """A narrowing key the parser cannot verify belongs to no registry entry.

        The provider refuses such a key mid-fetch, which would surface as one
        failing series in the daily batch long after the registry edit.
        """

        empty = _estat_payload([])
        for series in load_definitions().series:
            if series.provider != "estat":
                continue
            with self.subTest(series=series.series_id):
                parse_estat_json(series, empty, start=date(1970, 1, 1), end=date(2100, 1, 1))

    def test_estat_dashboard_series_pin_one_upstream_series_in_their_source_url(self) -> None:
        """The registry, not a fetch, is where a mis-pinned selector must be caught.

        A selector the provider would reject only shows up in the daily batch as one
        failing series, so the registry entries are checked here instead.
        """

        dashboard = [
            definition
            for definition in load_definitions().series
            if definition.provider == "estat_dashboard"
        ]
        self.assertNotEqual(dashboard, [])

        for definition in dashboard:
            with self.subTest(series_id=definition.series_id):
                selectors = estat_dashboard_selectors(definition)
                self.assertEqual(definition.frequency, "monthly")
                self.assertEqual(selectors["IndicatorCode"], definition.provider_series_id)
                self.assertEqual(selectors["Cycle"], "1")
                self.assertIn(selectors["IsSeasonalAdjustment"], {"1", "2"})

    def test_every_series_id_maps_to_a_single_provider(self) -> None:
        series_ids = [series.series_id for series in load_definitions().series]

        self.assertEqual(len(series_ids), len(set(series_ids)))

    def test_us_equity_indices_registered(self) -> None:
        by_id = load_definitions().by_id()
        for series_id in ("us.sp500", "us.nasdaq", "us.dow"):
            self.assertIn(series_id, by_id)
            self.assertEqual(by_id[series_id].category, "equity-index")
            self.assertEqual(by_id[series_id].provider, "fred_csv")

    def test_tradingview_symbols_cover_major_market_series(self) -> None:
        by_id = load_definitions().by_id()
        configured = {
            series_id: definition.tradingview_symbol
            for series_id, definition in by_id.items()
            if definition.tradingview_symbol is not None
        }

        self.assertGreaterEqual(len(configured), 10)
        self.assertEqual(configured["us.10y"], "TVC:US10Y")
        self.assertEqual(configured["usd_jpy"], "FX:USDJPY")
        self.assertEqual(configured["vix"], "CBOE:VIX")
        self.assertEqual(configured["jp.nikkei225"], "TVC:NI225")
        self.assertEqual(configured["us.sp500"], "SP:SPX")
        self.assertIsNone(by_id["jp.pmi_manufacturing"].tradingview_symbol)

    def test_tradingview_symbol_rejects_invalid_format(self) -> None:
        canonical = Path("engine/src/baibai_engine/macro/indicators/registry/us.yaml").read_text(
            encoding="utf-8"
        )
        for invalid in ("invalid symbol", ":", "TVC:", ":US10Y", "A:B:C"):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                definitions = Path(tmp) / "us.yaml"
                definitions.write_text(
                    canonical.replace(
                        "tradingview_symbol: TVC:US10Y",
                        f'tradingview_symbol: "{invalid}"',
                        1,
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ValueError, "EXCHANGE:SYMBOL"):
                    load_definitions(definitions)

    def test_registry_rejects_duplicate_series_id_across_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp)
            block = (
                "series:\n"
                "  - series_id: us.10y\n"
                "    name: dup\n"
                "    category: rates\n"
                "    geography: us\n"
                "    frequency: daily\n"
                "    unit: percent\n"
                "    provider: fred_csv\n"
                "    provider_series_id: DGS10\n"
                "    source_id: x\n"
                "    source_url: https://example.com/x.csv\n"
            )
            (registry / "a.yaml").write_text(block, encoding="utf-8")
            (registry / "b.yaml").write_text(block, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "duplicate indicator series_id.*us.10y"):
                load_definitions(registry)

    def test_registry_rejects_duplicate_yaml_mapping_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            definitions = Path(tmp) / "registry.yaml"
            definitions.write_text(
                "series:\n"
                "  - series_id: test.series\n"
                "    name: Test series\n"
                "    category: rates\n"
                "    geography: test\n"
                "    frequency: daily\n"
                "    unit: percent\n"
                "    provider: fred_csv\n"
                "    provider_series_id: TEST\n"
                "    source_id: test-source\n"
                "    source_url: https://example.com/test.csv\n"
                "    aliases: [first]\n"
                "    aliases: [second]\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate YAML mapping key: 'aliases'"):
                load_definitions(definitions)

    def test_registry_parses_optional_plausible_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            definitions = Path(tmp) / "registry.yaml"
            definitions.write_text(
                _registry_yaml(plausible_min="-2", plausible_max="20"),
                encoding="utf-8",
            )

            series = load_definitions(definitions).series[0]

            self.assertEqual(series.plausible_min, -2.0)
            self.assertEqual(series.plausible_max, 20.0)

    def test_registry_rejects_invalid_plausible_range(self) -> None:
        cases = (
            ("true", "20", "finite number"),
            ("low", "20", "finite number"),
            (".inf", "20", "finite number"),
            ("20", "-2", "less than or equal"),
        )
        for plausible_min, plausible_max, expected in cases:
            with self.subTest(plausible_min=plausible_min), tempfile.TemporaryDirectory() as tmp:
                definitions = Path(tmp) / "registry.yaml"
                definitions.write_text(
                    _registry_yaml(
                        plausible_min=plausible_min,
                        plausible_max=plausible_max,
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ValueError, expected):
                    load_definitions(definitions)

    def test_series_definition_rejects_invalid_programmatic_plausible_range(self) -> None:
        for plausible_min, plausible_max, expected in (
            (float("nan"), 20.0, "plausible_min must be a finite number"),
            (-2.0, float("inf"), "plausible_max must be a finite number"),
            (True, 20.0, "plausible_min must be a finite number"),
            (20.0, -2.0, "plausible_min must be less than or equal"),
        ):
            with (
                self.subTest(plausible_min=plausible_min, plausible_max=plausible_max),
                self.assertRaisesRegex(ValueError, expected),
            ):
                _series(
                    "fred_csv",
                    "TEST",
                    plausible_min=plausible_min,
                    plausible_max=plausible_max,
                )

    def test_registry_rejects_alias_colliding_with_other_canonical_identity(self) -> None:
        for conflicting_alias in ("second.series", "Second series"):
            with (
                self.subTest(conflicting_alias=conflicting_alias),
                tempfile.TemporaryDirectory() as tmp,
            ):
                definitions = Path(tmp) / "registry.yaml"
                definitions.write_text(
                    "series:\n"
                    "  - series_id: first.series\n"
                    "    name: First series\n"
                    "    category: rates\n"
                    "    geography: test\n"
                    "    frequency: daily\n"
                    "    unit: percent\n"
                    "    provider: fred_csv\n"
                    "    provider_series_id: FIRST\n"
                    "    source_id: first-source\n"
                    "    source_url: https://example.com/first.csv\n"
                    f"    aliases: [{conflicting_alias}]\n"
                    "  - series_id: second.series\n"
                    "    name: Second series\n"
                    "    category: rates\n"
                    "    geography: test\n"
                    "    frequency: daily\n"
                    "    unit: percent\n"
                    "    provider: fred_csv\n"
                    "    provider_series_id: SECOND\n"
                    "    source_id: second-source\n"
                    "    source_url: https://example.com/second.csv\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "indicator alias collides with another series_id or name",
                ):
                    load_definitions(definitions)

    def test_every_registered_series_provider_is_registered(self) -> None:
        from baibai_engine.macro.indicators.providers import provider_spec

        for series in load_definitions().series:
            with self.subTest(series_id=series.series_id):
                # resolve_provider (via provider_spec) raises for an unknown provider.
                self.assertEqual(provider_spec(series.provider).name, series.provider)

    def test_every_registered_series_declares_a_complete_plausible_range(self) -> None:
        for series in load_definitions().series:
            with self.subTest(series_id=series.series_id):
                self.assertIsNotNone(series.plausible_min)
                self.assertIsNotNone(series.plausible_max)

    def test_every_boj_series_binds_its_column_to_an_expected_header(self) -> None:
        boj_series = [series for series in load_definitions().series if series.provider == "boj"]

        self.assertGreater(len(boj_series), 0)
        for series in boj_series:
            with self.subTest(series_id=series.series_id):
                column, expected_header, metadata_column, expected_metadata = (
                    series.provider_series_id.split("|")
                )
                self.assertGreaterEqual(int(column), 2)
                self.assertTrue(expected_header.strip())
                self.assertGreaterEqual(int(metadata_column), 1)
                self.assertTrue(expected_metadata.strip())

    def test_derived_formula_and_registry_plausible_ranges_do_not_drift(self) -> None:
        registry = load_definitions().by_id()

        self.assertEqual(
            set(FORMULAS), {key for key, value in registry.items() if value.provider == "derived"}
        )
        for series_id, formula in FORMULAS.items():
            with self.subTest(series_id=series_id):
                series = registry[series_id]
                self.assertEqual(
                    (formula.plausible_min, formula.plausible_max),
                    (series.plausible_min, series.plausible_max),
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

            self.assertEqual(main(["search", "CPI", "--db", str(db)]), 0)
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
        self.assertEqual(main(["list", "--db", "/tmp"]), 1)

    def test_cli_unknown_series_returns_controlled_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"

            self.assertEqual(main(["get", "jp.cpi.stale", "--latest", "--db", str(db)]), 1)


def _write_retired_series(database: Path) -> None:
    """A store written while the series was still registered, then left behind.

    The generation is one behind the current registry because retiring a series
    changes the canonical membership, and only a client whose registry is newer
    than the store may prune what the store still holds.
    """

    conn = initialize_database(database)
    try:
        indicators_db.set_registry_generation(conn, load_definitions().generation - 1)
        conn.execute(
            "INSERT INTO series("
            "series_id, name, category, geography, frequency, unit, provider, "
            "provider_series_id, source_id, source_url"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "jp.cpi.stale",
                "stale",
                "inflation",
                "japan",
                "monthly",
                "index",
                "fred_csv",
                "JPNCPIALLMINMEI",
                "fred-jp-cpi",
                "https://example.com/stale.csv",
            ),
        )
        conn.execute(
            "INSERT INTO aliases(alias, series_id) VALUES (?, ?)",
            ("stale alias", "jp.cpi.stale"),
        )
        insert_observations(
            conn,
            [
                ObservationRecord(
                    series_id="jp.cpi.stale",
                    observed_at=date(2021, 12, 1),
                    value=100.0,
                    unit="index",
                    source_url="https://example.com/stale.csv",
                    vintage_at=datetime(2026, 5, 1, tzinfo=UTC),
                )
            ],
        )
        record_provider_run(
            conn,
            provider="fred_csv",
            series_id="jp.cpi.stale",
            start=date(2021, 1, 1),
            end=date(2021, 12, 31),
            started_at=datetime(2026, 5, 1, tzinfo=UTC),
            status="ok",
            record_count=1,
        )
        conn.commit()
    finally:
        conn.close()


def _retired_counts(database: Path) -> tuple[int, int, int]:
    conn = sqlite3.connect(database)
    try:
        return cast(
            tuple[int, int, int],
            tuple(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE series_id = ?",
                    ("jp.cpi.stale",),
                ).fetchone()[0]
                for table in ("series", "observations", "provider_runs")
            ),
        )
    finally:
        conn.close()


def _multpl_monthly_table(*, floor: date, latest: date, omit: date | None = None) -> str:
    """Render a multpl by-month table covering ``floor``..``latest`` inclusive.

    Month arithmetic is spelled out here rather than reused from the provider so a
    defect in the provider's month stepping cannot cancel itself out in the fixture.
    """
    rows: list[str] = []
    month = floor
    while month <= latest:
        if month != omit:
            rows.append(f"<tr><td>{month:%b} 1, {month.year}</td><td>20.0</td></tr>")
        month = (
            date(month.year + 1, 1, 1)
            if month.month == 12
            else month.replace(month=month.month + 1)
        )
    return '<table id="datatable">' + "".join(reversed(rows)) + "</table>"


def _series(
    provider: str,
    provider_series_id: str,
    *,
    series_id: str = "test.series",
    unit: str = "percent",
    frequency: str = "daily",
    plausible_min: float | None = None,
    plausible_max: float | None = None,
) -> SeriesDefinition:
    """A provider-specific registry entry; `category` stays "test" for these tests."""

    return series_definition(
        series_id,
        provider=provider,
        provider_series_id=provider_series_id,
        category="test",
        unit=unit,
        frequency=frequency,
        plausible_min=plausible_min,
        plausible_max=plausible_max,
    )


def _estat_payload(
    value: object,
    *,
    status: int = 0,
    next_key: int | None = None,
) -> str:
    result_inf: dict[str, object] = {"TOTAL_NUMBER": 1}
    if next_key is not None:
        result_inf["NEXT_KEY"] = next_key
    return json.dumps(
        {
            "GET_STATS_DATA": {
                "RESULT": {"STATUS": status},
                "STATISTICAL_DATA": {
                    "RESULT_INF": result_inf,
                    "DATA_INF": {"VALUE": value},
                },
            }
        }
    )


def _registry_yaml(*, plausible_min: str, plausible_max: str) -> str:
    return (
        "series:\n"
        "  - series_id: test.series\n"
        "    name: Test series\n"
        "    category: rates\n"
        "    geography: test\n"
        "    frequency: daily\n"
        "    unit: percent\n"
        "    provider: fred_csv\n"
        "    provider_series_id: TEST\n"
        "    source_id: test-source\n"
        "    source_url: https://example.com/test.csv\n"
        f"    plausible_min: {plausible_min}\n"
        f"    plausible_max: {plausible_max}\n"
    )


def _pmi_stream(months: list[str]) -> tuple[Release, ...]:
    return _parse_stream(
        "jp_manufacturing",
        [
            {
                "observed_at": month,
                "url": (f"https://www.pmi.spglobal.com/Public/Home/PressRelease/{index:032x}"),
            }
            for index, month in enumerate(months, start=1)
        ],
    )


def _pmi_stored(
    series_id: str, releases: Sequence[Release], values: Sequence[float]
) -> tuple[ObservationRecord, ...]:
    """Stored observations carrying the release URL their month names in the manifest."""
    return tuple(
        ObservationRecord(
            series_id=series_id,
            observed_at=release.observed_at,
            value=value,
            unit="index",
            source_url=release.url,
            vintage_at=datetime.now(UTC),
        )
        for release, value in zip(releases, values, strict=True)
    )


def _fetch_pmi_stream(
    series: SeriesDefinition,
    stream: tuple[Release, ...],
    *,
    start: date,
    end: date,
    context: FetchContext | None,
) -> tuple[list[ObservationRecord], list[str]]:
    """Run the PMI provider against a fixture manifest, recording what it fetched.

    Release text is stubbed per URL so the test stays on the fetch-selection and
    manifest-freshness behaviour instead of PDF parsing.
    """

    month_by_url = {release.url: release.observed_at for release in stream}
    fetched: list[str] = []

    def release_text(url: str, *, session: object, context: object) -> str:
        fetched.append(url)
        return f"the headline PMI posted 50.4 in {month_by_url[url].strftime('%B')}."

    with (
        patch.object(spglobal_pmi, "load_manifest", return_value={"jp_manufacturing": stream}),
        patch.object(spglobal_pmi, "release_text", side_effect=release_text),
    ):
        observations = SpGlobalPmiProvider().fetch(
            series,
            start=start,
            end=end,
            session=cast(HttpSession, _RaisingSession()),
            context=context,
        )
    return observations, fetched


def _boj_workbook_bytes(
    rows: list[tuple[object, ...]],
    *,
    header_column: int = 3,
    header_label: str = "Monetary Base",
    metadata_column: int = 8,
    metadata: str = "Unit: 100 million yen",
    other_metadata: tuple[int, str] | None = None,
) -> bytes:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    header = [None] * header_column
    header[header_column - 1] = header_label
    worksheet.append(header)
    metadata_width = max(
        metadata_column,
        other_metadata[0] if other_metadata is not None else 0,
    )
    metadata_row: list[object] = [None] * metadata_width
    metadata_row[metadata_column - 1] = metadata
    if other_metadata is not None:
        metadata_row[other_metadata[0] - 1] = other_metadata[1]
    worksheet.append(metadata_row)
    for row in rows:
        worksheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _write_observation_with_coverage(
    db: Path,
    series_id: str,
    *,
    observed_at: date,
    value: float,
    coverage_start: date,
    coverage_end: date,
) -> None:
    _write_observation(db, series_id, observed_at=observed_at, value=value)
    conn = sqlite3.connect(db)
    try:
        record_provider_run(
            conn,
            provider="test",
            series_id=series_id,
            start=coverage_start,
            end=coverage_end,
            started_at=datetime.now(UTC),
            status="ok",
            record_count=1,
        )
        conn.commit()
    finally:
        conn.close()


def _write_observation(
    db: Path,
    series_id: str,
    *,
    observed_at: date,
    value: float,
    vintage_at: datetime | None = None,
) -> None:
    conn = initialize_database(db)
    try:
        series = get_series(conn, series_id)
        insert_observations(
            conn,
            [
                ObservationRecord(
                    series_id=series_id,
                    observed_at=observed_at,
                    value=value,
                    unit=series.unit,
                    source_url=series.source_url,
                    vintage_at=vintage_at or datetime.now(UTC),
                )
            ],
        )
        conn.commit()
    finally:
        conn.close()


_DASHBOARD_INDICATOR = "0301010000020020010"
_DASHBOARD_SOURCE_URL = (
    "https://dashboard.e-stat.go.jp/api/1.0/Json/getData"
    f"?Lang=JP&IndicatorCode={_DASHBOARD_INDICATOR}"
    "&Cycle=1&IsSeasonalAdjustment=2&RegionCode=00000"
)
_DASHBOARD_SELECTORS = {
    "Lang": "JP",
    "IndicatorCode": _DASHBOARD_INDICATOR,
    "Cycle": "1",
    "IsSeasonalAdjustment": "2",
    "RegionCode": "00000",
}


def _dashboard_series(
    *,
    source_url: str = _DASHBOARD_SOURCE_URL,
    frequency: str = "monthly",
) -> SeriesDefinition:
    return replace(
        _series("estat_dashboard", _DASHBOARD_INDICATOR, frequency=frequency),
        source_url=source_url,
    )


def _dashboard_row(time_code: str, value: str) -> dict[str, dict[str, str]]:
    return {
        "VALUE": {
            "@indicator": _DASHBOARD_INDICATOR,
            "@unit": "001",
            "@stat": "00200531",
            "@regionCode": "00000",
            "@time": time_code,
            "@cycle": "1",
            "@regionRank": "2",
            "@isSeasonal": "2",
            "@isProvisional": "0",
            "$": value,
        }
    }


def _dashboard_payload(
    rows: Sequence[Mapping[str, Mapping[str, str]]],
    *,
    total: int | None = None,
) -> str:
    return json.dumps(
        {
            "GET_STATS": {
                "RESULT": {"status": "0", "errorMsg": "正常に終了しました。"},
                "STATISTICAL_DATA": {
                    "RESULT_INF": {"TOTAL_NUMBER": str(len(rows) if total is None else total)},
                    "TABLE_INF": {},
                    "DATA_INF": {"DATA_OBJ": list(rows)},
                },
            }
        }
    )


def _parse_dashboard(
    rows: Sequence[Mapping[str, Mapping[str, str]]],
    *,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 12, 31),
) -> list[ObservationRecord]:
    return parse_dashboard_json(
        _dashboard_series(),
        _dashboard_payload(rows),
        start=start,
        end=end,
        selectors=_DASHBOARD_SELECTORS,
    )


class _ForbiddenResponse:
    """A 403 as an edge bot-mitigation serves it: a challenge page, not an error."""

    status_code = 403
    headers: dict[str, str] = {"Content-Type": "text/html"}

    def raise_for_status(self) -> None:
        raise requests.HTTPError("403 Client Error: Forbidden")

    def iter_content(self, *, chunk_size: int) -> list[bytes]:
        return [b"<!DOCTYPE html><html><title>Just a moment...</title></html>"]


class _FakeBrowserFetcher:
    """Stands in for the headless browser so a fallback is testable without Playwright.

    Returns whatever bytes it is given rather than only well-formed CSV, so a
    caller that skips validating the browser's answer is visible in a test.
    """

    def __init__(self, result: bytes | Exception) -> None:
        self.result = result
        self.urls: list[str] = []
        self.max_bytes: list[int] = []

    def fetch_download(self, url: str, *, max_bytes: int) -> bytes:
        self.urls.append(url)
        self.max_bytes.append(max_bytes)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def fetch_pdf(self, url: str, *, max_bytes: int) -> bytes:
        self.urls.append(url)
        self.max_bytes.append(max_bytes)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self) -> None:
        return None


def _context_with_browser(browser: _FakeBrowserFetcher) -> FetchContext:
    """A fetch context whose browser is already the fake, so none is ever launched."""

    context = FetchContext()
    context._browser = cast("BrowserFetcher", browser)
    return context


class _RecordingSession:
    """Captures the query a provider sends so the request contract is testable."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.params: dict[str, str] | None = None

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: int,
        stream: bool = False,
    ) -> _FakeResponse:
        self.params = params
        return _FakeResponse(self.body.encode("utf-8"))


class _RaisingSession:
    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: int,
        stream: bool = False,
    ) -> object:
        raise requests.Timeout("simulated timeout")


class _StaticSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: int,
        stream: bool = False,
    ) -> _FakeResponse:
        return self.response


class _FakeResponse:
    def __init__(
        self,
        content: bytes,
        *,
        headers: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> None:
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int) -> list[bytes]:
        return [self.content]
