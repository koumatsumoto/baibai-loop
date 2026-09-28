from __future__ import annotations

from datetime import UTC, date, datetime
from typing import cast
from unittest.mock import patch

from tests.engine.macro.indicator_fixtures import (
    _context_with_browser,
    _FakeBrowserFetcher,
    _FakeResponse,
    _fetch_pmi_stream,
    _ForbiddenResponse,
    _pmi_stored,
    _pmi_stream,
    _RaisingSession,
    _series,
    _StaticSession,
)
from tests.engine.macro.provider_fixtures import SnapshotFixtures

from baibai_engine.macro.indicators.db import (
    ObservationRecord,
)
from baibai_engine.macro.indicators.definitions import (
    load_definitions,
)
from baibai_engine.macro.indicators.providers import (
    IndicatorsProviderError,
    spglobal_pmi,
)
from baibai_engine.macro.indicators.providers.base import (
    FetchContext,
    HttpSession,
)
from baibai_engine.macro.indicators.providers.pmi_extraction import (
    PmiExtractionError,
    extract_pmi_value,
)
from baibai_engine.macro.indicators.providers.spglobal_pmi import (
    MANIFEST_PATH,
    MAX_PMI_PDF_BYTES,
    SpGlobalPmiProvider,
    _parse_stream,
    extract_pdf_text,
    load_manifest,
    release_text,
)


class IndicatorsProviderParserTests(SnapshotFixtures):
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
