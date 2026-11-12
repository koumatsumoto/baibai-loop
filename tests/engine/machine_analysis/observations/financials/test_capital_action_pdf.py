import json
from datetime import UTC, date, datetime
from pathlib import Path

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
        b"%PDF-synthetic", source_url=URL, received_at=CLOCK, as_of=CLOCK.date()
    )


def test_completed_conversion_keeps_source_and_does_not_manufacture_treasury(monkeypatch):
    value = parse(monkeypatch, HEADER + COMPLETED + TOTAL)
    assert value is not None
    assert (value.ticker, value.issued_shares, value.effective_date.isoformat()) == (
        "5838",
        223936153,
        "2026-10-01",
    )
    assert value.published_on == date(2026, 10, 1)
    assert value.source_url == URL
    assert not hasattr(value, "treasury_shares")


@pytest.mark.parametrize(
    "content",
    [
        HEADER + TOTAL,
        HEADER + "本日、本株式交付の効力が発生する予定です。" + TOTAL,
        HEADER + "本日、本株式交付の効力が発生します。" + TOTAL,
        HEADER + COMPLETED + TOTAL + TOTAL,
        HEADER + COMPLETED + COMPLETED + TOTAL,
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


@pytest.fixture
def subsequent_fixture():
    path = Path(__file__).parents[4] / "fixtures/jpx/7640_completed_post_period_conversion.json"
    return json.loads(path.read_text())


def parse_subsequent(monkeypatch, fixture, *, texts=None, as_of=date(2026, 11, 12)):
    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class Reader:
        def __init__(self, data):
            self.pages = [Page(text) for text in (fixture["texts"] if texts is None else texts)]

    monkeypatch.setattr(subject, "PdfReader", Reader)
    return subject.extract_completed_conversion(
        b"%PDF-synthetic-excerpts",
        source_url=fixture["source_url"],
        received_at=datetime(2026, 11, 12, 12, tzinfo=UTC),
        as_of=as_of,
    )


def test_completed_subsequent_event_separates_effective_disclosure_and_receipt(
    monkeypatch, subsequent_fixture
):
    action = parse_subsequent(monkeypatch, subsequent_fixture)
    assert action is not None
    assert (action.ticker, action.issued_shares, action.effective_date) == (
        "7640",
        17972552,
        date(2026, 8, 31),
    )
    assert action.published_on == date(2026, 9, 10)
    assert action.received_at == datetime(2026, 11, 12, 12, tzinfo=UTC)
    assert action.disclosure_number == "20260909533860"
    assert action.description == "completed_post_period_common_share_conversion_total"
    assert not hasattr(action, "treasury_shares")
    assert parse_subsequent(monkeypatch, subsequent_fixture, as_of=date(2026, 9, 1)) is None
    assert parse_subsequent(monkeypatch, subsequent_fixture, as_of=date(2026, 9, 10)) == action


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("消却いたしました", "消却する予定です"),
        ("2026年8月31日付で", "2026年8月30日付で"),
        ("2026年8月31日", "2026年9月11日"),
        ("2026年8月31日", "2026年2月31日"),
        ("17,972,552株", "0株"),
        ("17,972,552株", "17,972,552株(予定)"),
        ("1,758,152株", "1,758,152株(見込み)"),
        ("取得請求権行使日2026年8月31日", "取得請求権行使日2026年8月31日(予定)"),
        ("転換の内容", "転換の内容(未定)"),
        ("当社は、", "当社の連結子会社は、"),
        ("当社が発行する", "当社の連結子会社が発行する"),
        ("取得株式の種類株式会社トップカルチャー", "取得株式の種類株式会社別の連結子会社"),
        ("上場会社名株式会社トップカルチャー", "上場会社名株式会社別の親会社"),
        ("1,758,152株", "0株"),
        ("コード番号7640", "コード番号76400"),
        ("取得するA種優先株式数", "取得するB種優先株式数"),
        ("増加後の発行済み普通株式総数", "転換後の株数見込み"),
    ],
)
def test_subsequent_plans_mismatches_invalid_or_missing_totals_are_unclassified(
    monkeypatch, subsequent_fixture, before, after
):
    texts = [text.replace(before, after) for text in subsequent_fixture["texts"]]
    assert texts != subsequent_fixture["texts"]
    assert parse_subsequent(monkeypatch, subsequent_fixture, texts=texts) is None


def test_multiple_completed_events_are_not_silently_combined(monkeypatch, subsequent_fixture):
    texts = subsequent_fixture["texts"]
    assert parse_subsequent(monkeypatch, subsequent_fixture, texts=[*texts, texts[1]]) is None
    mixed = [texts[0], texts[1] + COMPLETED + TOTAL]
    assert parse_subsequent(monkeypatch, subsequent_fixture, texts=mixed) is None


def test_late_disclosure_reader_respects_both_publication_and_exact_receipt_cutoffs(
    tmp_path, monkeypatch, subsequent_fixture
):
    action = parse_subsequent(monkeypatch, subsequent_fixture)
    assert action is not None
    receipt = action.received_at
    url = subsequent_fixture["source_url"]
    resource = "pdf:20260909533860"
    with open_connection(tmp_path / "market.sqlite") as conn:
        bundle = create_bundle(conn, "capital_actions", receipt.date(), receipt, ((resource, url),))
        retain_bundle_response(
            conn,
            bundle,
            resource,
            JPXResponse(url, receipt, "application/pdf", b"%PDF-synthetic-excerpts"),
        )
        complete_bundle(conn, bundle)
        assert not subject.read_capital_actions(
            conn, as_of=date(2026, 9, 1), tickers=["7640"], known_at=receipt
        )
        assert not subject.read_capital_actions(
            conn,
            as_of=date(2026, 11, 12),
            tickers=["7640"],
            known_at=receipt.replace(hour=11, minute=59, second=59, microsecond=999999),
        )
        assert subject.read_capital_actions(
            conn, as_of=date(2026, 11, 12), tickers=["7640"], known_at=receipt
        )["7640"] == (action,)
