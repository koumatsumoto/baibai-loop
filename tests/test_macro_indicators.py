from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import patch

import openpyxl
import requests

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.macro.indicators.cli import build_parser, main
from baibai_engine.macro.indicators.db import (
    SQLITE_SCHEMA_VERSION,
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
from baibai_engine.macro.indicators.definitions import SeriesDefinition, load_definitions
from baibai_engine.macro.indicators.providers import (
    IndicatorsProviderError,
    fetch_observations,
    parse_boj_timeseries_json,
    parse_boj_xlsx,
    parse_ecb_fx_csv,
    parse_estat_json,
    parse_fred_csv,
    parse_h15_csv,
    parse_manual_entries,
    parse_manual_seed,
    parse_mof_jgb_csv,
    parse_multpl_current,
    parse_multpl_history,
    parse_trades_spec,
    parse_tsr_bankruptcies_json,
    parse_yahoo_chart,
)
from baibai_engine.macro.indicators.providers.base import HttpSession
from baibai_engine.macro.indicators.providers.frb_h15 import FrbH15Provider
from baibai_engine.macro.indicators.providers.manual import MANUAL_DATA_PATH
from baibai_engine.macro.indicators.service import IndicatorsService


class IndicatorsDBTests(unittest.TestCase):
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
                            unit="jpy",
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

    def test_parse_manual_entries_filters_range_inclusive(self) -> None:
        series = _series("manual", "jp_pmi_manufacturing", unit="index")
        raw = {
            "schema_version": 1,
            "observations": [
                _manual_entry("test.series", "2026-01-01", 49.6, unit="index"),
                _manual_entry("test.series", "2026-02-01", 48.9, unit="index"),
                _manual_entry("test.series", "2026-03-01", 50.1, unit="index"),
            ],
        }

        observations = parse_manual_entries(
            series, raw, start=date(2026, 2, 1), end=date(2026, 3, 1)
        )

        self.assertEqual(
            [obs.observed_at for obs in observations],
            [date(2026, 2, 1), date(2026, 3, 1)],
        )
        self.assertEqual(observations[0].value, 48.9)

    def test_parse_manual_entries_rejects_missing_series_id(self) -> None:
        series = _series("manual", "jp_unknown", unit="count")
        raw = {
            "schema_version": 1,
            "observations": [_manual_entry("other.series", "2026-01-01", 49.6)],
        }

        with self.assertRaisesRegex(IndicatorsProviderError, "test.series"):
            parse_manual_entries(series, raw, start=date(2026, 1, 1), end=date(2026, 12, 31))

    def test_parse_manual_entries_accepts_iso_string_date_and_int_value(self) -> None:
        series = _series("manual", "jp_bankruptcies_tsr", unit="count")
        raw = {
            "schema_version": 1,
            "observations": [_manual_entry("test.series", "2026-03-01", 950)],
        }

        observations = parse_manual_entries(
            series, raw, start=date(2026, 1, 1), end=date(2026, 12, 31)
        )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_at, date(2026, 3, 1))
        self.assertEqual(observations[0].value, 950.0)

    def test_canonical_manual_seed_matches_registry_and_preserves_all_vintages(self) -> None:
        raw = safe_load(MANUAL_DATA_PATH.read_text(encoding="utf-8"))

        observations = parse_manual_seed(load_definitions(), raw)

        self.assertEqual(len(observations), 36)
        self.assertEqual(
            {item.series_id for item in observations},
            {"jp.pmi_manufacturing"},
        )
        self.assertTrue(all(item.vintage_at is not None for item in observations))

    def test_manual_pmi_source_rejects_noncanonical_release_url(self) -> None:
        canonical = (
            "https://www.pmi.spglobal.com/Public/Home/PressRelease/4bfeffc263944cd797a7957577045b71"
        )
        for source_url in (
            canonical.replace("spglobal.com", "spglobal.com.evil.example"),
            f"{canonical}?download=1",
            f"{canonical}#release",
        ):
            with self.subTest(source_url=source_url):
                raw = safe_load(MANUAL_DATA_PATH.read_text(encoding="utf-8"))
                raw["observations"][0]["source_url"] = source_url
                with self.assertRaisesRegex(IndicatorsProviderError, "source_url differs"):
                    parse_manual_seed(load_definitions(), raw)

    def test_manual_pmi_source_must_match_release_manifest(self) -> None:
        raw = safe_load(MANUAL_DATA_PATH.read_text(encoding="utf-8"))
        raw["observations"][0]["source_url"] = (
            "https://www.pmi.spglobal.com/Public/Home/PressRelease/00000000000000000000000000000000"
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "does not match manifest"):
            parse_manual_seed(load_definitions(), raw)

    def test_manual_pmi_history_must_cover_release_manifest(self) -> None:
        raw = safe_load(MANUAL_DATA_PATH.read_text(encoding="utf-8"))
        raw["observations"].pop()

        with self.assertRaisesRegex(IndicatorsProviderError, "history differs from manifest"):
            parse_manual_seed(load_definitions(), raw)

    def test_manual_pmi_rejects_implausible_value(self) -> None:
        raw = safe_load(MANUAL_DATA_PATH.read_text(encoding="utf-8"))
        raw["observations"][0]["value"] = -999

        with self.assertRaisesRegex(IndicatorsProviderError, "outside plausible range"):
            parse_manual_seed(load_definitions(), raw)

    def test_parse_manual_seed_normalizes_offsets_and_rejects_same_instant(self) -> None:
        raw = safe_load(MANUAL_DATA_PATH.read_text(encoding="utf-8"))
        pmi = [
            item
            for item in raw["observations"]
            if item["series_id"] == "jp.pmi_manufacturing"
            and str(item["observed_at"]) == "2026-01-01"
        ]
        pmi.append(dict(pmi[0]))
        pmi[0]["entered_at"] = "2026-07-21T00:00:00+09:00"
        pmi[1]["entered_at"] = "2026-07-20T15:30:00+00:00"
        raw["observations"].append(pmi[1])

        observations = parse_manual_seed(load_definitions(), raw)

        first_date = [
            item
            for item in observations
            if item.series_id == "jp.pmi_manufacturing" and item.observed_at == date(2026, 1, 1)
        ]
        self.assertEqual(
            [item.vintage_at for item in first_date],
            [
                datetime(2026, 7, 20, 15, 0, tzinfo=UTC),
                datetime(2026, 7, 20, 15, 30, tzinfo=UTC),
            ],
        )

        pmi[1]["entered_at"] = "2026-07-20T15:00:00+00:00"
        with self.assertRaisesRegex(IndicatorsProviderError, "duplicate"):
            parse_manual_seed(load_definitions(), raw)

    def test_parse_manual_seed_rejects_unbounded_integer(self) -> None:
        raw = safe_load(MANUAL_DATA_PATH.read_text(encoding="utf-8"))
        raw["observations"][0]["value"] = 10**10000

        with self.assertRaisesRegex(IndicatorsProviderError, "must be finite"):
            parse_manual_seed(load_definitions(), raw)

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

        with self.assertRaisesRegex(IndicatorsProviderError, "not a .xlsx"):
            parse_boj_xlsx(series, b"not a zip", start=date(2026, 1, 1), end=date(2026, 12, 31))

    def test_parse_boj_xlsx_rejects_non_numeric_column_index(self) -> None:
        content = _boj_workbook_bytes([(None, date(2026, 1, 31), 1.0, 2.0)])
        series = _series("boj", "BS01'MABJMTA", unit="jpy-100m")

        with self.assertRaisesRegex(IndicatorsProviderError, "1-based column index"):
            parse_boj_xlsx(series, content, start=date(2026, 1, 1), end=date(2026, 12, 31))

    def test_parse_boj_xlsx_wraps_corrupt_zip_as_provider_error(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("not-a-workbook.txt", "garbage")
        series = _series("boj", "3", unit="jpy-100m")

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

        with self.assertRaisesRegex(IndicatorsProviderError, "GET_STATS_DATA"):
            parse_estat_json(series, "{}", start=date(2026, 1, 1), end=date(2026, 12, 31))

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

    def test_parse_tsr_bankruptcies_json_rejects_unparseable_monthly_entry(
        self,
    ) -> None:
        series = _series("tsr_bankruptcies", "jp_bankruptcies_tsr", unit="count")
        text = json.dumps(
            [
                {
                    "period_division": "月次",
                    "title": "月次の全国企業倒産状況",
                    "free_word": [],
                }
            ]
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "cannot parse monthly entry"):
            parse_tsr_bankruptcies_json(
                series,
                text,
                start=date(2003, 1, 1),
                end=date(2026, 6, 1),
            )

    def test_parse_tsr_bankruptcies_json_rejects_history_gap(self) -> None:
        series = _series("tsr_bankruptcies", "jp_bankruptcies_tsr", unit="count")
        text = json.dumps(
            [
                {
                    "period_division": "月次",
                    "title": "2026年4月の全国企業倒産990件",
                },
                {
                    "period_division": "月次",
                    "title": "2026年6月の全国企業倒産1,021件",
                },
            ]
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "missing monthly entries"):
            parse_tsr_bankruptcies_json(
                series,
                text,
                start=date(2026, 4, 1),
                end=date(2026, 6, 1),
            )

    def test_parse_tsr_bankruptcies_json_rejects_missing_history_floor(self) -> None:
        series = _series("tsr_bankruptcies", "jp_bankruptcies_tsr", unit="count")
        text = json.dumps(
            [
                {
                    "period_division": "月次",
                    "title": "2026年6月の全国企業倒産1,021件",
                }
            ]
        )

        with self.assertRaisesRegex(IndicatorsProviderError, "must start at 2003-01-01"):
            parse_tsr_bankruptcies_json(
                series,
                text,
                start=date(2003, 1, 1),
                end=date(2026, 7, 20),
            )

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
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy")
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
        self.assertEqual(observations[0].unit, "jpy")

    def test_parse_trades_spec_falls_back_to_purchases_minus_sales(self) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy")
        rows = [{"PubDate": "2026-05-08", "FrgnBuy": 1500, "FrgnSell": 1000}]

        observations = parse_trades_spec(series, rows, start=date(2026, 5, 8), end=date(2026, 5, 8))

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].value, 500.0)

    def test_parse_trades_spec_keeps_each_period_for_duplicate_publication_date(
        self,
    ) -> None:
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy")
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
        series = _series("jquants_flows", "foreigners_net_value", unit="jpy")
        rows = [{"PubDate": "2026-05-08", "Section": "TSEPrime"}]

        with self.assertRaisesRegex(IndicatorsProviderError, "missing"):
            parse_trades_spec(series, rows, start=date(2026, 5, 8), end=date(2026, 5, 8))

    def test_fetch_observations_rejects_unknown_provider(self) -> None:
        series = _series("nonexistent_provider", "X")

        with self.assertRaisesRegex(
            IndicatorsProviderError, "unsupported indicator provider: nonexistent_provider"
        ):
            fetch_observations(series, start=date(2026, 5, 1), end=date(2026, 5, 1))


class IndicatorsRegistryTests(unittest.TestCase):
    def test_new_tier1_series_registered_with_expected_provider_and_category(self) -> None:
        by_id = load_definitions().by_id()
        expected = {
            "jp.nikkei225": ("fred_csv", "equity-index"),
            "jp.policy_rate": ("boj_timeseries", "policy"),
            "jp.10y": ("mof_jgb", "rates"),
            "jp.unemployment": ("fred_csv", "labor"),
            "jp.hourly_earnings": ("fred_csv", "labor"),
            "jp.real_effective_exchange_rate": ("fred_csv", "fx"),
            "jp.cpi.services": ("estat", "inflation"),
            "credit.us_hy_oas": ("fred_csv", "credit"),
            "credit.us_ccc_oas": ("fred_csv", "credit"),
            "btc_usd": ("fred_csv", "crypto"),
        }

        for series_id, (provider, category) in expected.items():
            self.assertIn(series_id, by_id)
            self.assertEqual(by_id[series_id].provider, provider)
            self.assertEqual(by_id[series_id].category, category)

        self.assertEqual(
            by_id["jp.hourly_earnings"].provider_series_id,
            "LCEAMN01JPM661S",
        )
        self.assertEqual(
            by_id["jp.real_effective_exchange_rate"].provider_series_id,
            "RBJPBIS",
        )
        self.assertEqual(
            by_id["jp.cpi.services"].provider_series_id,
            "0003427113?cdCat01=0220&cdArea=00000&cdTab=1",
        )

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
        canonical = Path("src/baibai_engine/macro/indicators/registry/us.yaml").read_text(
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

    def test_every_registered_series_provider_is_registered(self) -> None:
        from baibai_engine.macro.indicators.providers import provider_spec

        for series in load_definitions().series:
            with self.subTest(series_id=series.series_id):
                # resolve_provider (via provider_spec) raises for an unknown provider.
                self.assertEqual(provider_spec(series.provider).name, series.provider)


class IndicatorsServiceTests(unittest.TestCase):
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

    def test_refresh_all_history_uses_jquants_light_window(self) -> None:
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
                            unit="jpy",
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
                unit="jpy",
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

            self.assertEqual(fetch.call_args.kwargs["start"], date(2021, 7, 20))
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
            self.assertEqual(
                observed_dates,
                ["2021-06-01", "2025-06-01", "2025-12-26"],
            )

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

    def test_refresh_all_history_excludes_manual_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"

            with self.assertRaisesRegex(IndicatorsProviderError, "import-manual"):
                IndicatorsService(database).refresh_all_history(
                    "jp.pmi_manufacturing",
                    end=date(2026, 7, 20),
                )

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

    def test_import_manual_seed_is_idempotent_and_replaces_manual_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"
            service = IndicatorsService(database)

            first = service.import_manual_seed()
            with sqlite3.connect(database) as connection:
                before = connection.execute(
                    """
                    SELECT series_id, observed_at, value, unit, vintage_at, source_url
                    FROM observations
                    WHERE series_id = 'jp.pmi_manufacturing'
                    ORDER BY series_id, observed_at, vintage_at
                    """
                ).fetchall()
                counts_before = (
                    connection.execute("SELECT count(*) FROM observations").fetchone()[0],
                    connection.execute("SELECT count(*) FROM provider_runs").fetchone()[0],
                )
            second = service.import_manual_seed()
            with sqlite3.connect(database) as connection:
                after = connection.execute(
                    """
                    SELECT series_id, observed_at, value, unit, vintage_at, source_url
                    FROM observations
                    WHERE series_id = 'jp.pmi_manufacturing'
                    ORDER BY series_id, observed_at, vintage_at
                    """
                ).fetchall()
                counts_after = (
                    connection.execute("SELECT count(*) FROM observations").fetchone()[0],
                    connection.execute("SELECT count(*) FROM provider_runs").fetchone()[0],
                )

            self.assertEqual(first.series_count, 1)
            self.assertEqual(first.observation_count, 36)
            self.assertEqual(second, first)
            self.assertEqual(after, before)
            self.assertEqual(counts_after, counts_before)

    def test_import_manual_seed_rejects_unknown_series_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "macro.sqlite"
            seed = root / "manual.yaml"
            raw = safe_load(MANUAL_DATA_PATH.read_text(encoding="utf-8"))
            raw["observations"].append(_manual_entry("jp.unknown", "2026-01-01", 1))
            seed.write_text(json.dumps(raw, default=str), encoding="utf-8")

            with self.assertRaisesRegex(IndicatorsProviderError, "unknown series jp.unknown"):
                IndicatorsService(database).import_manual_seed(seed)

            self.assertFalse(database.exists())

    def test_import_manual_seed_rejects_duplicate_yaml_key_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "macro.sqlite"
            seed = root / "manual.yaml"
            service = IndicatorsService(database)
            service.import_manual_seed()
            with sqlite3.connect(database) as connection:
                before = connection.execute(
                    "SELECT * FROM observations ORDER BY series_id, observed_at, vintage_at"
                ).fetchall()
            seed_text = MANUAL_DATA_PATH.read_text(encoding="utf-8")
            value_line = next(
                line
                for line in seed_text.splitlines(keepends=True)
                if line.lstrip().startswith("value: ")
            )
            seed.write_text(
                seed_text.replace(value_line, f"{value_line}{value_line}", 1),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate YAML mapping key: 'value'"):
                service.import_manual_seed(seed)

            with sqlite3.connect(database) as connection:
                after = connection.execute(
                    "SELECT * FROM observations ORDER BY series_id, observed_at, vintage_at"
                ).fetchall()
            self.assertEqual(after, before)

    def test_import_manual_seed_selects_latest_vintage_across_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "macro.sqlite"
            seed = root / "manual.yaml"
            raw = safe_load(MANUAL_DATA_PATH.read_text(encoding="utf-8"))
            pmi = [
                item
                for item in raw["observations"]
                if item["series_id"] == "jp.pmi_manufacturing"
                and str(item["observed_at"]) == "2026-01-01"
            ]
            pmi.append(dict(pmi[0]))
            raw["observations"].append(pmi[1])
            pmi[0].update(
                value=40,
                entered_at="2026-07-21T00:00:00+09:00",
            )
            pmi[1].update(
                value=60,
                entered_at="2026-07-20T23:00:00+00:00",
            )
            seed.write_text(json.dumps(raw, default=str), encoding="utf-8")
            service = IndicatorsService(database)

            service.import_manual_seed(seed)
            ranged = service.get_range(
                "jp.pmi_manufacturing",
                start=date(2026, 1, 1),
                end=date(2026, 1, 1),
            )
            latest = service.get_latest("jp.pmi_manufacturing")

            self.assertEqual(ranged.observations[0].value, 60)
            self.assertEqual(
                ranged.observations[0].vintage_at, datetime(2026, 7, 20, 23, tzinfo=UTC)
            )
            self.assertEqual(latest.observations[-1].observed_at, date(2026, 6, 1))
            with sqlite3.connect(database) as connection:
                stored_offsets = connection.execute(
                    """
                    SELECT DISTINCT substr(vintage_at, -6)
                    FROM observations
                    WHERE series_id = 'jp.pmi_manufacturing'
                    """
                ).fetchall()
            self.assertEqual(stored_offsets, [("+00:00",)])

    def test_import_manual_seed_rejects_rows_removed_from_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "macro.sqlite"
            seed = root / "manual.yaml"
            raw = safe_load(MANUAL_DATA_PATH.read_text(encoding="utf-8"))
            raw["observations"].pop(0)
            seed.write_text(json.dumps(raw, default=str), encoding="utf-8")
            service = IndicatorsService(database)

            with self.assertRaisesRegex(
                IndicatorsProviderError,
                "history differs from manifest",
            ):
                service.import_manual_seed(seed)
            self.assertFalse(database.exists())

    def test_import_manual_cli_reports_seed_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "macro.sqlite"

            self.assertEqual(main(["import-manual", "--db", str(database)]), 0)

            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM observations WHERE series_id = ?",
                        ("jp.pmi_manufacturing",),
                    ).fetchone()[0],
                    36,
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
            observed_at = datetime.now(UTC).date()
            _write_observation(
                db,
                "us.10y",
                observed_at=observed_at,
                value=4.45,
            )

            with patch(
                "baibai_engine.macro.indicators.service.fetch_observations",
                side_effect=AssertionError("provider should not be called"),
            ):
                result = IndicatorsService(db).get_latest("us.10y")

            self.assertTrue(result.cache_hit)
            self.assertEqual(result.observations[0].observed_at, observed_at)

    def test_get_latest_uses_short_daily_provider_lookback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            initialize_database(db).close()
            today = datetime.now(UTC).date()
            observation = ObservationRecord(
                series_id="jp.10y",
                observed_at=today,
                value=2.8,
                unit="percent",
                source_url="https://example.com/data.csv",
                vintage_at=datetime.now(UTC),
            )
            with patch(
                "baibai_engine.macro.indicators.service.fetch_observations",
                return_value=[observation],
            ) as fetch:
                result = IndicatorsService(db).get_latest("jp.10y", refresh=True)

            self.assertFalse(result.cache_hit)
            self.assertEqual(fetch.call_args.kwargs["start"], today - timedelta(days=14))

    def test_get_latest_refreshes_stale_latest_even_when_range_has_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.sqlite"
            today = datetime.now(UTC).date()
            _write_observation_with_coverage(
                db,
                "jp.10y",
                observed_at=today - timedelta(days=2),
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
            with patch(
                "baibai_engine.macro.indicators.service.fetch_observations",
                return_value=[observation],
            ) as fetch:
                result = IndicatorsService(db).get_latest("jp.10y")

            self.assertFalse(result.cache_hit)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(result.observations[0].observed_at, today - timedelta(days=1))
            self.assertEqual(result.observations[0].value, 2.8)

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


def _manual_entry(
    series_id: str,
    observed_at: str,
    value: int | float,
    *,
    unit: str = "count",
) -> dict[str, object]:
    return {
        "series_id": series_id,
        "observed_at": observed_at,
        "value": value,
        "unit": unit,
        "source_url": "https://example.com/data.csv",
        "entered_at": "2026-07-20T00:00:00+00:00",
    }


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
    def __init__(self, content: bytes, *, headers: dict[str, str] | None = None) -> None:
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int) -> list[bytes]:
        return [self.content]
