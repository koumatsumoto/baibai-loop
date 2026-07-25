from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import patch

import openpyxl
import requests

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
    parse_mof_jgb_csv,
    parse_multpl_current,
    parse_multpl_history,
    parse_trades_spec,
    parse_tsr_bankruptcies_json,
    parse_yahoo_chart,
    spglobal_pmi,
)
from baibai_engine.macro.indicators.providers.base import FetchContext, HttpSession
from baibai_engine.macro.indicators.providers.cftc import parse_cftc_json
from baibai_engine.macro.indicators.providers.derived import DerivedProvider
from baibai_engine.macro.indicators.providers.formulas import FORMULAS, DerivedComputationError
from baibai_engine.macro.indicators.providers.frb_h15 import FrbH15Provider
from baibai_engine.macro.indicators.providers.jquants_indices import parse_index_bars
from baibai_engine.macro.indicators.providers.nikkei_indexes import parse_nikkei_valuation
from baibai_engine.macro.indicators.providers.pmi_extraction import (
    PmiExtractionError,
    extract_pmi_value,
)
from baibai_engine.macro.indicators.providers.spglobal_pmi import (
    SpGlobalPmiProvider,
    _manifest,
    _parse_stream,
    _Release,
    extract_pdf_text,
)
from baibai_engine.macro.indicators.service import (
    IndicatorsService,
    RefreshFailure,
    RefreshSuccess,
)


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

    def test_extract_pmi_value_reads_headline_for_month(self) -> None:
        text = (
            "the headline S&P Global Japan Manufacturing PMI picked up to 54.8 in June "
            "from 54.5 in May and signalled an improvement in operating conditions."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2026, 6, 1),
            release_observed_at=date(2026, 6, 1),
        )

        self.assertEqual(value, 54.8)

    def test_extract_pmi_value_returns_none_when_month_absent(self) -> None:
        text = "the headline PMI picked up to 54.8 in June from 54.5 in May."

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2026, 3, 1),
            release_observed_at=date(2026, 6, 1),
        )

        self.assertIsNone(value)

    def test_extract_pmi_value_rejects_implausible_reading(self) -> None:
        text = "the headline PMI collapsed to 12.3 in June, an unprecedented reading."

        with self.assertRaisesRegex(PmiExtractionError, "outside plausible range"):
            extract_pmi_value(
                text,
                expected_observed_at=date(2026, 6, 1),
                release_observed_at=date(2026, 6, 1),
            )

    def test_extract_pmi_value_rejects_conflicting_values(self) -> None:
        text = (
            "the headline PMI reading was 54.8 in June. Separately, the headline PMI "
            "figure was 55.9 in June per a revised estimate."
        )

        with self.assertRaisesRegex(PmiExtractionError, "conflicting"):
            extract_pmi_value(
                text,
                expected_observed_at=date(2026, 6, 1),
                release_observed_at=date(2026, 6, 1),
            )

    def test_extract_pmi_value_reads_services_headline_without_pmi_token(self) -> None:
        # Services releases phrase the value as "the headline index posted X in
        # Month" — the value sentence names no "PMI", and a definitional
        # "the headline figure is ..." sentence comes first.
        text = (
            "The headline figure is the Services Business Activity Index, which tracks "
            "changes in the volume of business activity. A reading above 50.0 indicates "
            "growth. The headline index posted 53.2 in November, up fractionally from "
            "53.1 in October and signalled a further solid expansion."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2025, 11, 1),
            release_observed_at=date(2025, 11, 1),
        )

        self.assertEqual(value, 53.2)

    def test_extract_pmi_value_reads_index_anchored_statement_without_headline(self) -> None:
        # Older releases drop "the headline" and lead with the index name.
        text = (
            "The seasonally adjusted S&P Global US Services PMI® Business Activity Index "
            "posted 52.9 in January, down markedly from 56.8 in December."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2025, 1, 1),
            release_observed_at=date(2025, 1, 1),
        )

        self.assertEqual(value, 52.9)

    def test_extract_pmi_value_reads_value_with_qualifier_between_verb_and_number(self) -> None:
        # "posted at the neutral level of 50.0 in October" — a qualifier sits
        # between the reporting verb and the number.
        text = (
            "The seasonally adjusted S&P Global US Manufacturing Purchasing Managers' "
            "Index™ (PMI) posted at the neutral level of 50.0 in October, in line with "
            "the earlier flash estimate."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2023, 10, 1),
            release_observed_at=date(2023, 10, 1),
        )

        self.assertEqual(value, 50.0)

    def test_extract_pmi_value_reads_the_previous_month_named_in_a_restating_release(self) -> None:
        # A release states the month before it as well as its own, which is how a
        # month whose own release is unavailable is read.
        text = (
            "The headline index posted 53.2 in November, up fractionally from 53.1 "
            "in October and signalled a further solid expansion."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2025, 10, 1),
            release_observed_at=date(2025, 11, 1),
        )

        self.assertEqual(value, 53.1)

    def test_extract_pmi_value_reads_the_previous_month_from_a_bare_comparison(self) -> None:
        # The comparison names no month, so it can only be read for the month before
        # the one the release reports.
        text = (
            "The seasonally adjusted S&P Global US Manufacturing Purchasing Managers’ "
            "Index™ (PMI®) remained above the crucial 50.0 no-change mark in March. "
            "Recording 50.2, down from 52.7, the PMI signaled a marginal improvement."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2025, 2, 1),
            release_observed_at=date(2025, 3, 1),
        )

        self.assertEqual(value, 52.7)

    def test_extract_pmi_value_does_not_attribute_a_release_reading_to_the_previous_month(
        self,
    ) -> None:
        # The statement introduces its reading without naming a month, so it belongs
        # to the release's own month and must not answer for the month before it.
        text = "The headline index posted 53.2 in November, ending a soft patch."

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2025, 10, 1),
            release_observed_at=date(2025, 11, 1),
        )

        self.assertIsNone(value)

    def test_extract_pmi_value_reads_a_value_the_pdf_text_split_at_the_decimal(self) -> None:
        # pypdf renders "47.9" as "47 .9" in some releases.
        text = "The PMI fell to 47 .9 in August, from 49.0 in July, indicating a downturn."

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2023, 8, 1),
            release_observed_at=date(2023, 8, 1),
        )

        self.assertEqual(value, 47.9)

    def test_extract_pmi_value_reads_a_month_stated_before_its_value(self) -> None:
        # "in <month> to <value>" — the month leads the value in the same clause.
        text = (
            "The seasonally adjusted S&P Global US Services PMI ® Business Activity "
            "Index fell for the third month running in April to 51.3 from 51.7 in March."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2024, 4, 1),
            release_observed_at=date(2024, 4, 1),
        )

        self.assertEqual(value, 51.3)

    def test_extract_pmi_value_reads_a_value_stated_during_the_month(self) -> None:
        # "during <month>", and the flash estimate in the same sentence is provisional.
        text = (
            "The S&P Global US Services PMI® Business Activity Index recorded 53.7 "
            "during May, which was stronger than the earlier 'flash' reading of 52.3."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2025, 5, 1),
            release_observed_at=date(2025, 5, 1),
        )

        self.assertEqual(value, 53.7)

    def test_extract_pmi_value_reads_the_level_stated_after_the_threshold_sentence(self) -> None:
        # The statement sentence gives only the no-change threshold; the reading
        # follows in the next sentence.
        text = (
            "The headline au Jibun Bank Japan Services Business Activity Index remained "
            "above the 50.0 no-change mark for the fourteenth successive month in "
            "October, signalling a further expansion. That said, at 51.6 the index was "
            "down from 53.8 in September and pointed to a modest rise in output."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2023, 10, 1),
            release_observed_at=date(2023, 10, 1),
        )

        self.assertEqual(value, 51.6)

    def test_extract_pmi_value_reads_a_movement_destination_stated_after_a_comparison(
        self,
    ) -> None:
        # "slipped from <previous> in <previous month> to <reading>".
        text = (
            "However, the headline index slipped from 53.2 in November to 51.6, to "
            "signal a modest rate of growth that was the slowest seen since May."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2025, 12, 1),
            release_observed_at=date(2025, 12, 1),
        )

        self.assertEqual(value, 51.6)

    def test_extract_pmi_value_reads_a_reading_named_as_a_record_level(self) -> None:
        # "reaching a 33-month high of <reading> following a reading of <previous>".
        text = (
            "The seasonally adjusted S&P Global US Services PMI® Business Activity Index "
            "rose for the second month running in December, reaching a 33-month high of "
            "56.8 following a reading of 56.1 in November."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2024, 12, 1),
            release_observed_at=date(2024, 12, 1),
        )

        self.assertEqual(value, 56.8)

    def test_extract_pmi_value_reads_a_reading_equal_to_the_no_change_mark(self) -> None:
        # A reading of exactly 50.0 is stated as equal to the threshold.
        text = (
            "The seasonally adjusted S&P Global US Manufacturing Purchasing Managers’ "
            "Index™ (PMI ®) posted in line with the 50.0 no-change mark in April to "
            "point to stable business conditions."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2024, 4, 1),
            release_observed_at=date(2024, 4, 1),
        )

        self.assertEqual(value, 50.0)

    def test_extract_pmi_value_ignores_a_hedged_comparison_with_the_no_change_mark(self) -> None:
        # "broadly in line with" states an approximation, not the reading.
        text = (
            "The seasonally adjusted S&P Global US Manufacturing Purchasing Managers’ "
            "Index™ (PMI ®) was broadly in line with the 50.0 no-change mark in April."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2024, 4, 1),
            release_observed_at=date(2024, 4, 1),
        )

        self.assertIsNone(value)

    def test_extract_pmi_value_ignores_a_threshold_the_reading_is_measured_against(self) -> None:
        # "below the 50.0 no-change mark in November" is the threshold; the reading is
        # the level stated beside it.
        text = (
            "The seasonally adjusted S&P Global US Manufacturing Purchasing Managers’ "
            "Index™ (PMI®) remained below the 50.0 no-change mark in November, but at "
            "49.7 pointed to only a marginal worsening in the health of the sector."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2024, 11, 1),
            release_observed_at=date(2024, 11, 1),
        )

        self.assertEqual(value, 49.7)

    def test_extract_pmi_value_ignores_an_average_over_several_months(self) -> None:
        # A span average is not any single month's reading.
        text = (
            "The Business Activity Index has trended at 53.7 from January to November, "
            "comfortably above the next-highest annual average of 52.4 set in 2013."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2023, 11, 1),
            release_observed_at=date(2023, 11, 1),
        )

        self.assertIsNone(value)

    def test_extract_pmi_value_ignores_the_composite_index_statement(self) -> None:
        # The composite index is published in the same release and its statement reads
        # like the headline one.
        text = (
            "S&P Global US Services PMI® At 50.7 in November, the final S&P Global US "
            "Composite PMI Output Index* was unchanged from October. The headline S&P "
            "Global US Services PMI® Business Activity Index recorded 54.1 in November."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2025, 11, 1),
            release_observed_at=date(2025, 11, 1),
        )

        self.assertEqual(value, 54.1)

    def test_extract_pmi_value_ignores_a_sub_index_statement(self) -> None:
        # A sub-index moves on its own and must never answer for the headline.
        text = (
            "The headline index posted 49.7 in November, a marginal worsening. "
            "The New Orders Index rose to 48.2 in November."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2024, 11, 1),
            release_observed_at=date(2024, 11, 1),
        )

        self.assertEqual(value, 49.7)

    def test_extract_pmi_value_ignores_a_sentence_that_moves_on_from_the_statement(self) -> None:
        # Only a sentence that refers back to the index carries the statement on, so a
        # sentence about another subject cannot supply the headline reading.
        text = (
            "The headline index remained subdued in June. Employment growth eased to 51.2 in June."
        )

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2026, 6, 1),
            release_observed_at=date(2026, 6, 1),
        )

        self.assertIsNone(value)

    def test_extract_pmi_value_ignores_a_comparison_sharing_the_movement_preposition(
        self,
    ) -> None:
        # "compared to <previous>" borrows the preposition a movement uses, without
        # stating the release's own reading.
        text = "The headline index improved in October, compared to 52.0 in September."

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2025, 10, 1),
            release_observed_at=date(2025, 10, 1),
        )

        self.assertIsNone(value)

    def test_extract_pmi_value_ignores_a_year_beside_the_month(self) -> None:
        # A chart caption pairs the month with a year, which is not a reading.
        text = "Comment January 2026 Index, sa, >50 = growth m/m. The headline index eased."

        value = extract_pmi_value(
            text,
            expected_observed_at=date(2026, 1, 1),
            release_observed_at=date(2026, 1, 1),
        )

        self.assertIsNone(value)

    def test_extract_pdf_text_rejects_non_pdf(self) -> None:
        with self.assertRaisesRegex(IndicatorsProviderError, "not a PDF"):
            extract_pdf_text(b"<html>blocked</html>")

    def test_spglobal_pmi_manifest_parses_jp_manufacturing_stream(self) -> None:
        streams = _manifest()

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
        oldest = min(entry.observed_at for entries in _manifest().values() for entry in entries)

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

        streams = set(_manifest())
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
            patch.object(spglobal_pmi, "_manifest", return_value={"jp_manufacturing": stream}),
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
                _obs("us.10y", date(2026, 7, 16), 4.53),
                _obs("us.10y", date(2026, 7, 17), 4.55),
            ),
            # jp.10y missing 07-16 -> that date is skipped (no half-computed value)
            "jp.10y": (_obs("jp.10y", date(2026, 7, 17), 2.715),),
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

    def test_derived_provider_monthly_alignment_uses_month_end_of_daily_input(self) -> None:
        series = _series("derived", "jp.real_10y_proxy", unit="percent", frequency="monthly")
        store: dict[str, tuple[ObservationRecord, ...]] = {
            "jp.10y": (
                _obs("jp.10y", date(2026, 5, 1), 1.50),
                _obs("jp.10y", date(2026, 5, 29), 1.58),  # May month-end reading
                _obs("jp.10y", date(2026, 6, 1), 1.60),
                _obs("jp.10y", date(2026, 6, 30), 1.62),  # June month-end reading
            ),
            "jp.cpi.core_yoy": (
                _obs("jp.cpi.core_yoy", date(2026, 5, 1), 1.5),
                _obs("jp.cpi.core_yoy", date(2026, 6, 1), 1.6),
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

        # Each month pairs the month-end nominal yield with that month's core-CPI
        # YoY and emits the proxy at the first of the month.
        self.assertEqual(
            [(obs.observed_at, round(obs.value, 3)) for obs in observations],
            [(date(2026, 5, 1), 0.08), (date(2026, 6, 1), 0.02)],
        )

    def test_derived_provider_monthly_alignment_skips_month_missing_an_input(self) -> None:
        series = _series("derived", "jp.real_10y_proxy", unit="percent", frequency="monthly")
        store: dict[str, tuple[ObservationRecord, ...]] = {
            "jp.10y": (
                _obs("jp.10y", date(2026, 5, 29), 1.58),
                _obs("jp.10y", date(2026, 6, 30), 1.62),  # June has a yield ...
            ),
            # ... but June core CPI is not released yet, so June must not emit a
            # half-computed proxy.
            "jp.cpi.core_yoy": (_obs("jp.cpi.core_yoy", date(2026, 5, 1), 1.5),),
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
                return [_obs(series.series_id, date(2026, 7, 20), 4.2)]

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
                side_effect=lambda series, **_: [_obs(series.series_id, date(2026, 7, 20), 4.2)],
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
                return [_obs(series.series_id, date(2026, 7, 20), 4.2)]

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
                return [_obs(series.series_id, date(2026, 7, 20), 4.2)]

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
                return [_obs(series.series_id, date(2026, 7, 20), 4.2)]

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
                        return_value=[_obs("us.10y", date(2026, 7, 20), value)],
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
                return [_obs(series.series_id, date(2026, 7, 20), 4.2)]

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


def _obs(series_id: str, observed_at: date, value: float) -> ObservationRecord:
    return ObservationRecord(
        series_id=series_id,
        observed_at=observed_at,
        value=value,
        unit="percent",
        source_url="https://example.com/data.csv",
        vintage_at=datetime.now(UTC),
    )


def _series(
    provider: str,
    provider_series_id: str,
    *,
    unit: str = "percent",
    frequency: str = "daily",
) -> SeriesDefinition:
    return SeriesDefinition(
        series_id="test.series",
        name="Test Series",
        category="test",
        geography="world",
        frequency=frequency,
        unit=unit,
        provider=provider,
        provider_series_id=provider_series_id,
        source_id="test-source",
        source_url="https://example.com/data.csv",
    )


def _pmi_stream(months: list[str]) -> tuple[_Release, ...]:
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
    series_id: str, releases: Sequence[_Release], values: Sequence[float]
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
    stream: tuple[_Release, ...],
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

    def _release_text(url: str, *, session: object, context: object) -> str:
        fetched.append(url)
        return f"the headline PMI posted 50.4 in {month_by_url[url].strftime('%B')}."

    with (
        patch.object(spglobal_pmi, "_manifest", return_value={"jp_manufacturing": stream}),
        patch.object(spglobal_pmi, "_release_text", side_effect=_release_text),
    ):
        observations = SpGlobalPmiProvider().fetch(
            series,
            start=start,
            end=end,
            session=cast(HttpSession, _RaisingSession()),
            context=context,
        )
    return observations, fetched


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
