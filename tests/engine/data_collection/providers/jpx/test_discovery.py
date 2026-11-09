import pytest

from baibai_engine.data_collection.providers.jpx.discovery import EARNINGS_INDEX_URL, earnings_urls
from baibai_engine.data_collection.providers.jpx.models import JPXAcquisitionError


def test_earnings_index_extracts_unique_allowed_workbooks_with_cp932():
    html = """<p>決算</p><a href="kessan.xlsx">one</a><a href="kessan.xlsx">two</a>
    <a href="https://other.example/kessan.xlsx">external</a><a href="other.xlsx">other</a>"""
    assert earnings_urls(
        html.encode("cp932"), EARNINGS_INDEX_URL, "text/html; charset=shift_jis"
    ) == ("https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan.xlsx",)


def test_earnings_index_rejects_absent_or_foreign_links():
    with pytest.raises(JPXAcquisitionError):
        earnings_urls(b'<a href="https://other.example/kessan.xlsx">x</a>', EARNINGS_INDEX_URL)
