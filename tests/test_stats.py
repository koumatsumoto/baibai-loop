from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

from baibai_loop.stats.cli import main
from baibai_loop.stats.db import (
    SQLITE_SCHEMA_VERSION,
    ObservationRecord,
    get_series,
    initialize_database,
    insert_observations,
    list_series,
    observations_in_range,
    open_connection,
    record_provider_run,
    row_count,
)
from baibai_loop.stats.definitions import SeriesDefinition
from baibai_loop.stats.providers import parse_ecb_fx_csv, parse_fred_csv, parse_h15_csv
from baibai_loop.stats.service import StatsService


class StatsDBTests(unittest.TestCase):
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
            self.assertGreater(alias_count, 0)

    def test_open_connection_prunes_series_removed_from_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            conn = initialize_database(db)
            try:
                conn.execute(
                    "INSERT INTO series("
                    "series_id, name, domain, geography, frequency, unit, provider, "
                    "provider_series_id, source_id, source_url"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "jp.cpi.headline",
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
                    ("stale alias", "jp.cpi.headline"),
                )
                insert_observations(
                    conn,
                    [
                        ObservationRecord(
                            series_id="jp.cpi.headline",
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
                    series_id="jp.cpi.headline",
                    start=date(2021, 1, 1),
                    end=date(2021, 12, 31),
                    started_at=datetime(2026, 5, 1, tzinfo=UTC),
                    status="ok",
                    record_count=1,
                )
                conn.commit()
            finally:
                conn.close()

            conn = open_connection(db)
            try:
                series_ids = {series.series_id for series in list_series(conn)}
                stale_rows = conn.execute(
                    "SELECT COUNT(*) FROM observations WHERE series_id = ?",
                    ("jp.cpi.headline",),
                ).fetchone()[0]
                stale_runs = conn.execute(
                    "SELECT COUNT(*) FROM provider_runs WHERE series_id = ?",
                    ("jp.cpi.headline",),
                ).fetchone()[0]
                stale_aliases = conn.execute(
                    "SELECT COUNT(*) FROM aliases WHERE series_id = ?",
                    ("jp.cpi.headline",),
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertNotIn("jp.cpi.headline", series_ids)
            self.assertEqual(stale_rows, 0)
            self.assertEqual(stale_runs, 0)
            self.assertEqual(stale_aliases, 0)

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


class StatsProviderParserTests(unittest.TestCase):
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


class StatsServiceTests(unittest.TestCase):
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

            result = StatsService(db).get_range(
                "us.10y",
                start=date(2026, 5, 1),
                end=date(2026, 5, 15),
            )

            self.assertTrue(result.cache_hit)
            self.assertEqual(len(result.observations), 1)
            self.assertEqual(result.observations[0].value, 4.45)

    def test_brief_fragment_uses_cached_world_weekly_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            for series_id, previous, current in (
                ("us.10y", 4.40, 4.41),
                ("us.2y", 3.88, 3.92),
                ("us.10y_2y_spread", 52.0, 49.0),
                ("vix", 16.99, 17.19),
                ("brent", 113.89, 118.26),
                ("wti", 99.89, 109.76),
                ("usd_jpy", 156.56, 156.76),
                ("eur_jpy", 183.21, 184.37),
                ("aud_jpy", 111.91, 113.40),
            ):
                _write_observation_with_coverage(
                    db,
                    series_id,
                    observed_at=date(2026, 5, 2),
                    value=previous,
                    coverage_start=date(2026, 4, 6),
                    coverage_end=date(2026, 5, 10),
                )
                _write_observation(
                    db,
                    series_id,
                    observed_at=date(2026, 5, 8),
                    value=current,
                )

            payload = StatsService(db).brief_fragment(
                kind="world-weekly",
                start=date(2026, 5, 4),
                end=date(2026, 5, 10),
            )

            names = {item["name"] for item in payload["layers"]["world"]["market_indicators"]}
            self.assertIn("WTI原油", names)
            self.assertIn("USD/JPY", {item["name"] for item in payload["layers"]["japan"]["fx"]})
            self.assertIn(
                "WTI原油",
                {item["indicator"] for item in payload["deltas"]["threshold_breaches"]},
            )

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


def _series(provider: str, provider_series_id: str, *, unit: str = "percent") -> SeriesDefinition:
    return SeriesDefinition(
        series_id="test.series",
        name="Test Series",
        domain="test",
        geography="world",
        frequency="daily",
        unit=unit,
        provider=provider,
        provider_series_id=provider_series_id,
        source_id="test-source",
        source_url="https://example.com/data.csv",
    )


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
                    vintage_at=datetime.now(UTC),
                )
            ],
        )
        conn.commit()
    finally:
        conn.close()
