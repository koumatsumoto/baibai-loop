from __future__ import annotations

from contextlib import closing
from datetime import date
from typing import cast
from unittest import TestCase
from unittest.mock import patch

from tests.helpers.provider_fixtures import (
    _FakeResponse,
    _ForbiddenResponse,
    _StaticSession,
)

from baibai_engine.data_collection.catalog import load_sources
from baibai_engine.data_collection.providers.spglobal.pmi import (
    MANIFEST_PATH,
    Release,
    SpGlobalPmiSource,
    _parse_stream,
    fetch_pdf_bytes,
    load_manifest,
)
from baibai_engine.data_collection.transport.context import (
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
)


class PmiSourceTests(TestCase):
    def test_spglobal_pmi_does_not_bypass_a_source_refusal(self) -> None:
        with (
            FetchContext() as context,
            patch.object(context, "browser_fetcher") as browser,
            self.assertRaisesRegex(IndicatorsProviderError, "source withheld"),
        ):
            fetch_pdf_bytes(
                "https://www.pmi.spglobal.com/Public/Home/PressRelease/" + "a" * 32,
                session=cast(HttpSession, _StaticSession(_ForbiddenResponse())),
                context=context,
            )
        browser.assert_not_called()

    def test_spglobal_pmi_does_not_fall_back_for_a_failure_that_is_not_a_block(self) -> None:
        oversized = _FakeResponse(b"x" * 32, headers={"Content-Length": "9000000"})

        with (
            FetchContext() as context,
            patch.object(context, "browser_fetcher") as browser,
            self.assertRaisesRegex(IndicatorsProviderError, r"response too large"),
        ):
            fetch_pdf_bytes(
                "https://www.pmi.spglobal.com/Public/Home/PressRelease/" + "a" * 32,
                session=cast(HttpSession, _StaticSession(oversized)),
                context=context,
            )

        # A known size-limit failure is not a reason to repeat the GET through
        # another transport.
        browser.assert_not_called()

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


def test_rejected_shared_release_does_not_hide_later_month(tmp_path, monkeypatch) -> None:
    from baibai_engine.data_collection.acquisition.refresh import (
        SourceRefreshFailure,
        refresh_sources,
    )
    from baibai_engine.data_collection.receipts.native_sources.documents import read_documents
    from baibai_engine.data_collection.receipts.native_sources.store import (
        open_read_only_connection,
    )

    # A release can carry both this month's headline and the restated previous month.
    months = [date(2026, m, 1) for m in (5, 6, 7, 8)]
    first, second = (
        f"https://www.pmi.spglobal.com/Public/Home/PressRelease/{c * 32}" for c in "ab"
    )
    releases = (
        Release(months[0], months[1], first),
        Release(months[1], months[1], first),
        Release(months[2], months[3], second),
        Release(months[3], months[3], second),
    )
    monkeypatch.setattr(
        "baibai_engine.data_collection.providers.spglobal.pmi.load_manifest",
        lambda _: {"jp_manufacturing": releases},
    )
    # One bad HTTP 200 for May, then a real response at the June request.
    first_responses = iter((b"<html>unavailable</html>", b"%PDF-1.7 june"))
    calls = []

    def get(_session, url, **kwargs):
        calls.append(url)
        return _FakeResponse(next(first_responses) if url == first else b"%PDF-1.7 august")

    monkeypatch.setattr("requests.Session.get", get)
    path = tmp_path / "source.sqlite"
    source = "spglobal-jp-pmi-mfg"
    outcome = refresh_sources([source], db_path=path, start=months[0], end=months[-1])[0]
    assert isinstance(outcome, SourceRefreshFailure)
    assert "1 document/manifest failures" in outcome.message
    assert calls == [first, first, second]
    with closing(open_read_only_connection(path)) as conn:
        assert {doc.request_key: doc.payload for doc in read_documents(conn, source)} == {
            "2026-06-01": b"%PDF-1.7 june",
            "2026-07-01": b"%PDF-1.7 august",
            "2026-08-01": b"%PDF-1.7 august",
        }
