from __future__ import annotations

from datetime import date
from typing import cast
from unittest import TestCase

from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import (
    _context_with_browser,
    _FakeBrowserFetcher,
    _FakeResponse,
    _ForbiddenResponse,
    _StaticSession,
)

from baibai_engine.data_collection.catalog import load_sources
from baibai_engine.data_collection.providers.spglobal.pmi import (
    MANIFEST_PATH,
    SpGlobalPmiSource,
    _parse_stream,
    fetch_pdf_bytes,
    load_manifest,
)
from baibai_engine.data_collection.transport.context import (
    HttpSession,
    IndicatorsProviderError,
)


class PmiSourceTests(TestCase):
    def test_spglobal_pmi_does_not_bypass_a_source_refusal(self) -> None:
        browser = _FakeBrowserFetcher(b"%PDF unused")
        with (
            _context_with_browser(browser) as context,
            self.assertRaisesRegex(IndicatorsProviderError, "source withheld"),
        ):
            fetch_pdf_bytes(
                "https://www.pmi.spglobal.com/Public/Home/PressRelease/" + "a" * 32,
                session=cast(HttpSession, _StaticSession(_ForbiddenResponse())),
                context=context,
            )
        self.assertEqual(browser.urls, [])

    def test_spglobal_pmi_does_not_fall_back_for_a_failure_that_is_not_a_block(self) -> None:
        browser = _FakeBrowserFetcher(b"%PDF-1.4 unused")
        oversized = _FakeResponse(b"x" * 32, headers={"Content-Length": "9000000"})

        with (
            _context_with_browser(browser) as context,
            self.assertRaisesRegex(IndicatorsProviderError, r"response too large"),
        ):
            fetch_pdf_bytes(
                "https://www.pmi.spglobal.com/Public/Home/PressRelease/" + "a" * 32,
                session=cast(HttpSession, _StaticSession(oversized)),
                context=context,
            )

        # A known size-limit failure is not a reason to repeat the GET through
        # another transport.
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

        self.assertLessEqual(SpGlobalPmiSource.spec.all_history_start, oldest)

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

    def test_spglobal_pmi_manifest_covers_every_catalog_source(self) -> None:
        # L1 coverage does not depend on whether L2 already uses the source.
        # Every active acquisition request must have its own release stream.

        streams = set(load_manifest(MANIFEST_PATH))
        pmi_sources = [s for s in load_sources().values() if s.provider == "spglobal_pmi"]
        self.assertTrue(pmi_sources)
        for source in pmi_sources:
            self.assertIn(
                source.provider_series_id,
                streams,
                f"{source.source_id} has no manifest stream {source.provider_series_id!r}",
            )
