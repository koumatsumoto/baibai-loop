"""ECB source CSV keeps both currency legs before L2 cross-rate calculation."""

import io
import zipfile
from datetime import date
from typing import cast
from unittest import TestCase

import pytest
from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.provider_fixtures import _FakeResponse, _source, _StaticSession

import baibai_engine.data_collection.providers.ecb.fx as ecb
from baibai_engine.data_collection.providers.ecb.fx import parse_ecb_reference_csv
from baibai_engine.data_collection.transport.context import HttpSession, IndicatorsProviderError


class NativeParserTests(TestCase):
    def test_parse_ecb_reference_csv_preserves_all_native_currencies(self) -> None:
        text = "Date,USD,JPY,AUD,CHF\n2026-05-08,1.1761,184.37,1.6259,N/A\n"
        records = parse_ecb_reference_csv(text, start=date(2026, 5, 8), end=date(2026, 5, 8))
        self.assertEqual(
            records[date(2026, 5, 8)][0],
            {
                "Date": "2026-05-08",
                "USD": "1.1761",
                "JPY": "184.37",
                "AUD": "1.6259",
                "CHF": "N/A",
            },
        )

    def test_parse_ecb_reference_csv_rejects_missing_date_column(self) -> None:
        with self.assertRaisesRegex(IndicatorsProviderError, "missing Date column"):
            parse_ecb_reference_csv("JPY\n184.37\n", start=date(2026, 5, 8), end=date(2026, 5, 8))


def archive_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return buffer.getvalue()


def fetch_archive(payload):
    return ecb.EcbFxSource().fetch_records(
        _source("ecb_fx"),
        start=date(2026, 5, 8),
        end=date(2026, 5, 8),
        session=cast(HttpSession, _StaticSession(_FakeResponse(payload))),
    )


def test_zip_receiver_reads_one_named_csv_without_interpreting_missing_currency():
    payload = archive_bytes(
        [("data/eurofxref-hist.csv", "Date,USD,JPY,CHF\n2026-05-08,1.1,180,N/A\n")]
    )
    assert fetch_archive(payload) == {
        date(2026, 5, 8): [{"Date": "2026-05-08", "USD": "1.1", "JPY": "180", "CHF": "N/A"}]
    }


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ("not_zip", "not a ZIP archive"),
        ("missing_csv", "exactly one"),
        ("duplicate_csv", "exactly one"),
        ("size", "CSV too large"),
        ("ratio", "compression ratio"),
    ],
)
def test_zip_receiver_preserves_existing_archive_boundaries(monkeypatch, damage, reason):
    entries = [("eurofxref-hist.csv", "Date,USD,JPY\n2026-05-08,1.1,180\n")]
    if damage == "missing_csv":
        entries = [("other.csv", entries[0][1])]
    elif damage == "duplicate_csv":
        entries.append(("nested/eurofxref-hist.csv", entries[0][1]))
    elif damage == "size":
        monkeypatch.setattr(ecb, "MAX_ECB_CSV_BYTES", 10)
    elif damage == "ratio":
        monkeypatch.setattr(ecb, "MAX_ZIP_COMPRESSION_RATIO", 1)
        entries = [("eurofxref-hist.csv", "x" * 1000)]
    payload = b"not a ZIP" if damage == "not_zip" else archive_bytes(entries)
    with pytest.raises(IndicatorsProviderError, match=reason):
        fetch_archive(payload)
