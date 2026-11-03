from datetime import UTC, datetime

import pytest

from baibai_engine.data_collection.providers.jpx.models import JPXResponse
from baibai_engine.data_collection.providers.jpx.receipts import (
    complete_bundle,
    create_bundle,
    retain_bundle_response,
)
from baibai_engine.machine_analysis.observations.financials import capital_action_pdf as subject
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

URL = "https://www2.jpx.co.jp/disc/58380/140120260930543771.pdf"
CLOCK = datetime(2026, 10, 2, tzinfo=UTC)
HEADER = "2026 年 10 月 1 日 （コード：5838 東証プライム市場）"
COMPLETED = "本日、本株式交付の効力が発生いたしました。"
TOTAL = "本転換の結果、当行普通株式の発行済株式数は 223,936,153 株となり"


def parse(monkeypatch, content):
    class Page:
        def extract_text(self):
            return content

    class Reader:
        def __init__(self, data):
            self.pages = [Page()]

    monkeypatch.setattr(subject, "PdfReader", Reader)
    return subject.extract_completed_conversion(
        b"%PDF-synthetic", source_url=URL, received_at=CLOCK
    )


def test_completed_conversion_keeps_source_and_does_not_manufacture_treasury(monkeypatch):
    value = parse(monkeypatch, HEADER + COMPLETED + TOTAL)
    assert value is not None
    assert (value.ticker, value.issued_shares, value.effective_date.isoformat()) == (
        "5838",
        223936153,
        "2026-10-01",
    )
    assert value.source_url == URL
    assert not hasattr(value, "treasury_shares")


@pytest.mark.parametrize(
    "content",
    [
        HEADER + TOTAL,
        HEADER + "本日、本株式交付の効力が発生する予定です。" + TOTAL,
        HEADER + COMPLETED + TOTAL + TOTAL,
        HEADER.replace("5838", "9999") + COMPLETED + TOTAL,
    ],
)
def test_unconfirmed_or_ambiguous_action_is_not_observed(monkeypatch, content):
    assert parse(monkeypatch, content) is None


def test_reader_only_uses_retained_evidence_known_at_cutoff(tmp_path, monkeypatch):
    action = parse(monkeypatch, HEADER + COMPLETED + TOTAL)
    assert action is not None
    monkeypatch.setattr(subject, "extract_completed_conversion", lambda *args, **kwargs: action)
    with open_connection(tmp_path / "market.sqlite") as conn:
        bundle = create_bundle(
            conn, "capital_actions", CLOCK.date(), CLOCK, (("pdf:20260930543771", URL),)
        )
        retain_bundle_response(
            conn,
            bundle,
            "pdf:20260930543771",
            JPXResponse(URL, CLOCK, "application/pdf", b"%PDF-fake"),
        )
        complete_bundle(conn, bundle)
        assert not subject.read_capital_actions(
            conn, as_of=CLOCK.date(), tickers=["5838"], known_at=CLOCK.replace(day=1)
        )
        assert not subject.read_capital_actions(
            conn, as_of=CLOCK.date().replace(day=30, month=9), tickers=["5838"], known_at=CLOCK
        )
        assert subject.read_capital_actions(
            conn, as_of=CLOCK.date(), tickers=["5838"], known_at=CLOCK
        )["5838"] == (action,)
