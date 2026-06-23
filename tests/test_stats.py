from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import openpyxl
import requests

from baibai_loop.macro.indicators.cli import main
from baibai_loop.macro.indicators.db import (
    SQLITE_SCHEMA_VERSION,
    ObservationRecord,
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
from baibai_loop.macro.indicators.definitions import SeriesDefinition, load_definitions
from baibai_loop.macro.indicators.providers import (
    StatsProviderError,
    fetch_observations,
    parse_boj_xlsx,
    parse_ecb_fx_csv,
    parse_estat_json,
    parse_fred_csv,
    parse_h15_csv,
    parse_manual_entries,
    parse_trades_spec,
)
from baibai_loop.macro.indicators.service import StatsService


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

            self.assertNotIn("jp.cpi.stale", series_ids)
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

    def test_parse_h15_csv_rejects_missing_required_column(self) -> None:
        series = _series("frb_h15", "RIFLGFCY10_N.B")
        text = '"Time Period",OTHER\n2026-05-01,4.39\n'

        with self.assertRaisesRegex(StatsProviderError, "missing column RIFLGFCY10_N.B"):
            parse_h15_csv(series, text, start=date(2026, 5, 1), end=date(2026, 5, 1))

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

        with self.assertRaisesRegex(StatsProviderError, "missing column"):
            parse_ecb_fx_csv(series, text, start=date(2026, 5, 8), end=date(2026, 5, 8))

    def test_fetch_observations_wraps_request_exception(self) -> None:
        series = _series("fred_csv", "DGS10")

        with self.assertRaisesRegex(StatsProviderError, "failed to fetch"):
            fetch_observations(
                series,
                start=date(2026, 5, 1),
                end=date(2026, 5, 1),
                session=_RaisingSession(),
            )

    def test_fetch_observations_rejects_oversized_response(self) -> None:
        series = _series("fred_csv", "DGS10")

        with self.assertRaisesRegex(StatsProviderError, "too large"):
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

    def test_parse_manual_entries_filters_range_inclusive(self) -> None:
        series = _series("manual", "jp_pmi_manufacturing", unit="index")
        raw = {
            "jp_pmi_manufacturing": [
                {"date": date(2026, 1, 1), "value": 49.6},
                {"date": date(2026, 2, 1), "value": 48.9},
                {"date": date(2026, 3, 1), "value": 50.1},
            ]
        }

        observations = parse_manual_entries(
            series, raw, start=date(2026, 2, 1), end=date(2026, 3, 1)
        )

        self.assertEqual(
            [obs.observed_at for obs in observations],
            [date(2026, 2, 1), date(2026, 3, 1)],
        )
        self.assertEqual(observations[0].value, 48.9)

    def test_parse_manual_entries_rejects_unknown_provider_series_id(self) -> None:
        series = _series("manual", "jp_unknown", unit="count")
        raw = {"jp_pmi_manufacturing": [{"date": date(2026, 1, 1), "value": 49.6}]}

        with self.assertRaisesRegex(StatsProviderError, "jp_unknown"):
            parse_manual_entries(series, raw, start=date(2026, 1, 1), end=date(2026, 12, 31))

    def test_parse_manual_entries_accepts_iso_string_date_and_int_value(self) -> None:
        series = _series("manual", "jp_bankruptcies_tsr", unit="count")
        raw = {"jp_bankruptcies_tsr": [{"date": "2026-03-01", "value": 950}]}

        observations = parse_manual_entries(
            series, raw, start=date(2026, 1, 1), end=date(2026, 12, 31)
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 3, 1))
        self.assertEqual(observations[0].value, 950.0)

    def test_parse_boj_xlsx_extracts_value_column_and_filters_range(self) -> None:
        content = _boj_workbook_bytes(
            [
                (None, date(2026, 1, 31), 350000.0, 9999.0),
                (None, date(2026, 2, 28), 360000.0, 9999.0),
                (None, date(2026, 3, 31), 370000.0, 9999.0),
            ]
        )
        series = _series("boj", "3", unit="jpy-100m")

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
        series = _series("boj", "3", unit="jpy-100m")

        observations = parse_boj_xlsx(
            series, content, start=date(2026, 1, 1), end=date(2026, 12, 31)
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].value, 360000.0)

    def test_parse_boj_xlsx_rejects_non_xlsx_bytes(self) -> None:
        series = _series("boj", "3", unit="jpy-100m")

        with self.assertRaisesRegex(StatsProviderError, "not a .xlsx"):
            parse_boj_xlsx(series, b"not a zip", start=date(2026, 1, 1), end=date(2026, 12, 31))

    def test_parse_boj_xlsx_rejects_non_numeric_column_index(self) -> None:
        content = _boj_workbook_bytes([(None, date(2026, 1, 31), 1.0, 2.0)])
        series = _series("boj", "BS01'MABJMTA", unit="jpy-100m")

        with self.assertRaisesRegex(StatsProviderError, "1-based column index"):
            parse_boj_xlsx(series, content, start=date(2026, 1, 1), end=date(2026, 12, 31))

    def test_parse_boj_xlsx_wraps_corrupt_zip_as_provider_error(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("not-a-workbook.txt", "garbage")
        series = _series("boj", "3", unit="jpy-100m")

        with self.assertRaisesRegex(StatsProviderError, "could not be read"):
            parse_boj_xlsx(
                series, buffer.getvalue(), start=date(2026, 1, 1), end=date(2026, 12, 31)
            )

    def test_parse_estat_json_filters_range_and_skips_nonnumeric(self) -> None:
        series = _series("estat", "0003427113", unit="index")
        text = json.dumps(
            {
                "GET_STATS_DATA": {
                    "STATISTICAL_DATA": {
                        "DATA_INF": {
                            "VALUE": [
                                {"@time": "2026000101", "$": "100.1"},
                                {"@time": "2026000202", "$": "100.8"},
                                {"@time": "2026000303", "$": "-"},
                                {"@time": "2026000404", "$": "101.3"},
                            ]
                        }
                    }
                }
            }
        )

        observations = parse_estat_json(series, text, start=date(2026, 2, 1), end=date(2026, 3, 31))

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 2, 1))
        self.assertEqual(observations[0].value, 100.8)

    def test_parse_estat_json_handles_single_value_object(self) -> None:
        series = _series("estat", "0003427113", unit="index")
        text = json.dumps(
            {
                "GET_STATS_DATA": {
                    "STATISTICAL_DATA": {
                        "DATA_INF": {"VALUE": {"@time": "2026000505", "$": "102.0"}}
                    }
                }
            }
        )

        observations = parse_estat_json(
            series, text, start=date(2026, 1, 1), end=date(2026, 12, 31)
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 5, 1))
        self.assertEqual(observations[0].value, 102.0)

    def test_parse_estat_json_rejects_missing_structure(self) -> None:
        series = _series("estat", "0003427113", unit="index")

        with self.assertRaisesRegex(StatsProviderError, "GET_STATS_DATA"):
            parse_estat_json(series, "{}", start=date(2026, 1, 1), end=date(2026, 12, 31))

    def test_split_stats_data_id_extracts_narrowing_params(self) -> None:
        from baibai_loop.macro.indicators.providers.estat import _split_stats_data_id

        stats_id, narrowing = _split_stats_data_id("0003427113?cdCat01=0001&cdArea=00000&cdTab=1")

        self.assertEqual(stats_id, "0003427113")
        self.assertEqual(narrowing, {"cdCat01": "0001", "cdArea": "00000", "cdTab": "1"})

    def test_split_stats_data_id_without_query_returns_empty_params(self) -> None:
        from baibai_loop.macro.indicators.providers.estat import _split_stats_data_id

        self.assertEqual(_split_stats_data_id("0003427113"), ("0003427113", {}))

    def test_parse_trades_spec_filters_range_and_uses_foreign_balance(self) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy")
        rows = [
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
            series, rows, start=date(2026, 5, 8), end=date(2026, 5, 15)
        )

        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0].observed_at, date(2026, 5, 8))
        self.assertEqual(observations[0].value, 500.0)
        self.assertEqual(observations[1].value, -800.0)
        self.assertEqual(observations[0].unit, "jpy")

    def test_parse_trades_spec_falls_back_to_purchases_minus_sales(self) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy")
        rows = [{"PubDate": "2026-05-08", "FrgnBuy": 1500, "FrgnSell": 1000}]

        observations = parse_trades_spec(series, rows, start=date(2026, 5, 8), end=date(2026, 5, 8))

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].value, 500.0)

    def test_parse_trades_spec_rejects_missing_foreign_columns(self) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy")
        rows = [{"PubDate": "2026-05-08", "Section": "TSEPrime"}]

        with self.assertRaisesRegex(StatsProviderError, "missing"):
            parse_trades_spec(series, rows, start=date(2026, 5, 8), end=date(2026, 5, 8))

    def test_fetch_observations_rejects_unknown_provider(self) -> None:
        series = _series("nonexistent_provider", "X")

        with self.assertRaisesRegex(
            StatsProviderError, "unsupported stats provider: nonexistent_provider"
        ):
            fetch_observations(series, start=date(2026, 5, 1), end=date(2026, 5, 1))


class StatsRegistryTests(unittest.TestCase):
    def test_new_tier1_series_registered_with_expected_provider_and_category(self) -> None:
        by_id = load_definitions().by_id()
        expected = {
            "jp.nikkei225": ("fred_csv", "equity-index"),
            "jp.policy_rate": ("fred_csv", "policy"),
            "jp.unemployment": ("fred_csv", "labor"),
            "credit.us_hy_oas": ("fred_csv", "credit"),
            "btc_usd": ("fred_csv", "crypto"),
        }

        for series_id, (provider, category) in expected.items():
            self.assertIn(series_id, by_id)
            self.assertEqual(by_id[series_id].provider, provider)
            self.assertEqual(by_id[series_id].category, category)

    def test_every_series_id_maps_to_a_single_provider(self) -> None:
        series_ids = [series.series_id for series in load_definitions().series]

        self.assertEqual(len(series_ids), len(set(series_ids)))


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

    def test_get_latest_uses_fresh_cached_observation_before_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            observed_at = datetime.now(UTC).date()
            _write_observation(
                db,
                "us.10y",
                observed_at=observed_at,
                value=4.45,
            )

            with patch(
                "baibai_loop.macro.indicators.service.fetch_observations",
                side_effect=AssertionError("provider should not be called"),
            ):
                result = StatsService(db).get_latest("us.10y")

            self.assertTrue(result.cache_hit)
            self.assertEqual(result.observations[0].observed_at, observed_at)

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


def _series(provider: str, provider_series_id: str, *, unit: str = "percent") -> SeriesDefinition:
    return SeriesDefinition(
        series_id="test.series",
        name="Test Series",
        category="test",
        geography="world",
        frequency="daily",
        unit=unit,
        provider=provider,
        provider_series_id=provider_series_id,
        source_id="test-source",
        source_url="https://example.com/data.csv",
    )


def _boj_workbook_bytes(rows: list[tuple[object, ...]]) -> bytes:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
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


class _RaisingSession:
    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
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
        timeout: int,
        stream: bool = False,
    ) -> _FakeResponse:
        return self.response


class _FakeResponse:
    def __init__(self, content: bytes, *, headers: dict[str, str] | None = None) -> None:
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int) -> list[bytes]:
        return [self.content]
