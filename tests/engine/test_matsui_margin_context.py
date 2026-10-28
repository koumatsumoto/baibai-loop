"""Synthetic current-page receipts only; these tests never contact Matsui."""

from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
import requests
from tests.engine.market_context.indicators.indicator_fixtures import _FakeResponse

from baibai_engine.data_collection.economic import acquisition, store
from baibai_engine.data_collection.economic.documents import (
    SourceDocument,
    read_documents,
    store_document,
)
from baibai_engine.data_collection.economic.providers import matsui_margin
from baibai_engine.data_collection.economic.providers.base import FetchContext
from baibai_engine.data_collection.economic.sources import load_sources, require_source
from baibai_engine.cli.economic_indicators import main as indicator_main
from baibai_engine.machine_analysis.observations.economic_indicators.service import IndicatorsService
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import load_definitions
from baibai_engine.machine_analysis.models.market_context.reading.compute import compute_reading
from baibai_engine.machine_analysis.observations.economic_indicators.documents import read_document_series
from baibai_engine.machine_analysis.observations.economic_indicators.validation import (
    UnavailableObservationError,
)
from baibai_engine.machine_analysis.generation.runners.market_reading import reading_snapshot
from baibai_engine.machine_analysis.models.market_context.reading.rules import load_reading_rules

SERIES_ID = "jp.matsui_margin_long_evaluation_pnl"
SOURCE_ID = "matsui-margin"
WHEN = datetime(2026, 10, 23, 15, 30, tzinfo=UTC)  # Saturday in JST.
DAY = date(2026, 10, 23)


def html(
    label="10/23(金)",
    value="-12.345",
    note="権利落ち日の評価損益率はみなし数値を使用しております。",
):
    # A separate market-wide value and the sell value must never be selected.
    return f"""<html><p>二市場 評価損益率 -99.0％</p>
    <div class="theme-bgc-green market-brand-contents">
    <h3 class="market-brand-title">信用残速報（松井証券内）</h3>
    <p class="market-brand-day">{label}</p>
    <div class="market-brand-table">
      <div class="market-brand-table-row"><h2>売り残</h2><dl>
        <div><dt>信用残</dt><dd><span>110.00</span>億円</dd></div>
        <div><dt>評価損益率</dt><dd><span>-88.0</span>％</dd></div></dl></div>
      <div class="market-brand-table-row"><h2>買い残</h2><dl>
        <div><dt>信用残</dt><dd><span>1,234.00</span>億円</dd></div>
        <div><dt>評価損益率</dt><dd><span>{value}</span>％</dd></div></dl></div>
      <div class="market-brand-table-row"><h2>倍率</h2><dl><dd>11.0倍</dd></dl></div>
    </div><p class="market-brand-contents-text">{note}</p></div></html>""".encode()


def document(*, when=WHEN, body=None):
    from zoneinfo import ZoneInfo

    receipt_day = when.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
    return SourceDocument(
        SOURCE_ID,
        receipt_day,
        require_source(SOURCE_ID).source_url,
        "text/html",
        when,
        html() if body is None else body,
        {"receipt_jst_date": receipt_day},
    )


def save(path, *documents):
    with closing(store.initialize_database(path)) as connection, connection:
        for item in documents:
            store_document(connection, item)


def test_collection_receives_once_for_any_range_and_preserves_html(tmp_path, monkeypatch):
    body = html(value="未公表")

    class Session:
        calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            assert url == require_source(SOURCE_ID).source_url
            assert kwargs["params"] is None
            return _FakeResponse(body)

        def close(self):
            pass

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return WHEN.astimezone(tz)

    context = FetchContext()
    context.session.close()
    session = Session()
    context.session = session
    monkeypatch.setattr(acquisition, "FetchContext", lambda **kwargs: context)
    monkeypatch.setattr(matsui_margin, "datetime", Clock)
    path = tmp_path / "data.sqlite"
    outcome = acquisition.refresh_sources(
        [SOURCE_ID, SOURCE_ID], db_path=path, start=date(2000, 1, 1), end=DAY
    )
    assert len(outcome) == 1
    assert isinstance(outcome[0], acquisition.SourceRefreshSuccess)
    assert outcome[0].record_count == session.calls == 1
    with closing(store.open_read_only_connection(path)) as connection:
        received = list(read_documents(connection, SOURCE_ID))
    assert received == [document(body=body)]
    assert b"-88.0" in received[0].payload
    with pytest.raises(UnavailableObservationError):
        IndicatorsService(path).get_range(SERIES_ID, start=DAY, end=DAY)


def test_collection_transport_failure_is_one_get_without_fallback(tmp_path, monkeypatch):
    class Session:
        calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            raise requests.Timeout("synthetic failure")

        def close(self):
            pass

    context = FetchContext()
    context.session.close()
    session = Session()
    context.session = session
    monkeypatch.setattr(acquisition, "FetchContext", lambda **kwargs: context)
    path = tmp_path / "data.sqlite"
    outcome = acquisition.refresh_sources([SOURCE_ID], db_path=path, start=DAY, end=DAY)
    assert isinstance(outcome[0], acquisition.SourceRefreshFailure)
    assert session.calls == 1
    with closing(store.open_read_only_connection(path)) as connection:
        assert list(read_documents(connection, SOURCE_ID)) == []


@pytest.mark.parametrize(
    ("published", "expected_value"), [("+1.234", 1.234), ("-123.456", -123.456)]
)
@pytest.mark.parametrize(
    ("when", "label", "expected"),
    [
        ("2026-10-22T11:00:00+00:00", "10/22(木)", "2026-10-22"),  # same day
        ("2027-01-01T01:00:00+00:00", "12/31(木)", "2026-12-31"),  # rollover
        ("2026-10-23T15:30:00+00:00", "10/10(土)", "2026-10-10"),  # -14 inclusive
        ("2028-03-01T01:00:00+00:00", "2/29(火)", "2028-02-29"),
    ],
)
def test_year_inference_and_original_sign(
    tmp_path, when, label, expected, published, expected_value
):
    path = tmp_path / "data.sqlite"
    save(path, document(when=datetime.fromisoformat(when), body=html(label, published)))
    result = IndicatorsService(path).get_range(
        SERIES_ID, start=date(2020, 1, 1), end=date(2030, 1, 1)
    )
    (value,) = result.observations
    assert value.value == expected_value
    assert value.unit == "percent"
    assert value.observed_at.isoformat() == expected
    assert value.quality_flags == ("year_inferred",)
    assert value.vintage_at == datetime.fromisoformat(when)
    assert any(label in item and "14日" in item for item in value.notes)
    assert any("母集団" in item and "詳細は未確認" in item for item in value.notes)


@pytest.mark.parametrize(
    "body",
    [
        html("10/23(木)"),
        html("10/25(日)"),
        html("10/9(金)"),
        html("2/30(月)"),
        html(value="未公表"),
        html(value="NaN"),
        html(value="-12.3倍"),
        html(note=""),
        html().replace("買い残".encode(), "買い残別集計".encode()),
        b"<html>changed page</html>",
        b"\xff",
        html() + html(),
    ],
)
def test_latest_unreadable_source_withholds_only_this_series(tmp_path, body):
    path = tmp_path / "data.sqlite"
    old = document(when=datetime(2026, 10, 23, 2, tzinfo=UTC))
    save(path, old, document(body=body))
    before = path.read_bytes()
    with pytest.raises(UnavailableObservationError):
        IndicatorsService(path).get_range(SERIES_ID, start=DAY, end=DAY)
    payload = reading_snapshot(path, asof=DAY)
    rows = {item["series_id"]: item for item in payload["series"]}
    assert rows[SERIES_ID]["latest_value"] is None
    assert rows[SERIES_ID]["flags"] == ["source_unreadable"]
    assert rows[SERIES_ID]["notes"]
    assert rows["jp.10y"]["flags"] == []  # unrelated readings still materialize
    assert path.read_bytes() == before


def test_latest_per_request_then_per_observation_and_weekend_freshness(tmp_path, capsys):
    path = tmp_path / "data.sqlite"
    first = document(when=datetime(2026, 10, 23, 2, tzinfo=UTC), body=html(value="-10"))
    corrected = document(when=datetime(2026, 10, 23, 8, tzinfo=UTC), body=html(value="-11"))
    saturday = document(body=html(value="-12"))
    sunday = document(when=datetime(2026, 10, 25, 2, tzinfo=UTC), body=html(value="-13"))
    # Save out of order: selection must depend on receipt time, never insertion order.
    save(path, sunday, saturday, corrected, first)
    series = load_definitions().by_id()[SERIES_ID]
    with closing(store.open_read_only_connection(path)) as connection:
        raw = list(read_documents(connection, SOURCE_ID))
        values = read_document_series(connection, series, start=DAY, end=DAY)
    assert len(raw) == 3
    assert len(values) == 1
    assert values[0].value == -13
    assert values[0].observed_at == DAY
    assert values[0].vintage_at == sunday.fetched_at
    payload = reading_snapshot(path, asof=date(2026, 10, 25))
    row = next(item for item in payload["series"] if item["series_id"] == SERIES_ID)
    assert row["staleness_days"] == 2
    assert row["observed_at"] == DAY.isoformat()
    assert row["flags"] == ["year_inferred"]
    assert row["read_semantics"]["known_at_applied"] is False
    assert row["read_semantics"]["point_in_time_guaranteed"] is False
    assert list(values[0].quality_flags) == ["year_inferred"]
    assert row["notes"] == [f"Series definition: {series.notes}", *values[0].notes]
    assert values[0].vintage_at.isoformat() == row["vintage_at"]
    assert row["percentile"] is None
    assert row["insufficient_history"]
    assert (
        indicator_main(
            ["get", SERIES_ID, "--db", str(path), "--start", str(DAY), "--end", str(DAY)]
        )
        == 0
    )
    assert "year_inferred" in capsys.readouterr().out


def test_identical_same_request_receipt_does_not_claim_latest_fetch_time(tmp_path):
    path = tmp_path / "data.sqlite"
    first = document(when=datetime(2026, 10, 23, 2, tzinfo=UTC))
    same = replace(first, fetched_at=datetime(2026, 10, 23, 8, tzinfo=UTC))
    save(path, first, same)
    (value,) = IndicatorsService(path).get_range(SERIES_ID, start=DAY, end=DAY).observations
    assert value.vintage_at == first.fetched_at
    assert any("選択保存原本" in item and "最新取得時刻ではない" in item for item in value.notes)


def test_bad_latest_observation_revision_is_a_gap_not_an_older_value(tmp_path):
    path = tmp_path / "data.sqlite"
    first = document(when=datetime(2026, 10, 23, 2, tzinfo=UTC))
    bad = document(body=html(value="未公表"))
    monday = document(when=datetime(2026, 10, 26, 8, tzinfo=UTC), body=html("10/26(月)", "-15"))
    save(path, first, bad, monday)
    result = IndicatorsService(path).get_range(SERIES_ID, start=DAY, end=date(2026, 10, 26))
    assert [(item.observed_at, item.value) for item in result.observations] == [
        (date(2026, 10, 26), -15)
    ]


def test_later_unpublished_observation_does_not_hide_an_earlier_requested_day(tmp_path):
    path = tmp_path / "data.sqlite"
    day = date(2026, 10, 22)
    first = document(when=datetime(2026, 10, 22, 8, tzinfo=UTC), body=html("10/22(木)", "-10"))
    later = document(when=datetime(2026, 10, 23, 8, tzinfo=UTC), body=html(value="未公表"))
    save(path, first, later)
    (value,) = IndicatorsService(path).get_range(SERIES_ID, start=day, end=day).observations
    assert value.value == -10
    assert value.observed_at == day
    assert value.vintage_at == first.fetched_at
    payload = reading_snapshot(path, asof=day)
    row = next(item for item in payload["series"] if item["series_id"] == SERIES_ID)
    assert row["latest_value"] == -10
    assert row["observed_at"] == day.isoformat()
    assert row["flags"] == ["year_inferred"]
    empty_day = date(2026, 10, 21)
    assert (
        IndicatorsService(path).get_range(SERIES_ID, start=empty_day, end=empty_day).observations
        == ()
    )


def test_latest_in_range_unpublished_observation_is_unavailable(tmp_path):
    path = tmp_path / "data.sqlite"
    first = document(when=datetime(2026, 10, 22, 8, tzinfo=UTC), body=html("10/22(木)", "-10"))
    bad = document(when=datetime(2026, 10, 23, 8, tzinfo=UTC), body=html(value="未公表"))
    later = document(when=datetime(2026, 10, 26, 8, tzinfo=UTC), body=html("10/26(月)", "-15"))
    save(path, first, bad, later)
    with pytest.raises(UnavailableObservationError):
        IndicatorsService(path).get_range(SERIES_ID, start=date(2026, 10, 22), end=DAY)
    payload = reading_snapshot(path, asof=DAY)
    row = next(item for item in payload["series"] if item["series_id"] == SERIES_ID)
    assert row["latest_value"] is None
    assert row["flags"] == ["source_unreadable"]


def test_later_unpublished_correction_to_requested_day_cannot_restore_old_value(tmp_path):
    path = tmp_path / "data.sqlite"
    day = date(2026, 10, 22)
    first = document(when=datetime(2026, 10, 22, 8, tzinfo=UTC), body=html("10/22(木)", "-10"))
    correction = document(
        when=datetime(2026, 10, 23, 8, tzinfo=UTC), body=html("10/22(木)", "未公表")
    )
    later = document(when=datetime(2026, 10, 26, 8, tzinfo=UTC), body=html("10/26(月)", "-15"))
    save(path, first, correction, later)
    with pytest.raises(UnavailableObservationError):
        IndicatorsService(path).get_range(SERIES_ID, start=day, end=day)
    payload = reading_snapshot(path, asof=day)
    row = next(item for item in payload["series"] if item["series_id"] == SERIES_ID)
    assert row["latest_value"] is None
    assert row["flags"] == ["source_unreadable"]


def test_latest_unresolvable_date_still_fails_closed_for_an_earlier_range(tmp_path):
    path = tmp_path / "data.sqlite"
    day = date(2026, 10, 22)
    first = document(when=datetime(2026, 10, 22, 8, tzinfo=UTC), body=html("10/22(木)", "-10"))
    bad = document(when=datetime(2026, 10, 23, 8, tzinfo=UTC), body=html("10/23(木)", "-11"))
    save(path, first, bad)
    with pytest.raises(UnavailableObservationError, match="observation date has no unique match"):
        IndicatorsService(path).get_range(SERIES_ID, start=day, end=day)


def test_matsui_source_is_registered_and_has_no_history_archive():
    assert SOURCE_ID in load_sources()
    assert matsui_margin.MatsuiMarginSource.spec.all_history_start is None


def test_unrelated_reader_errors_are_not_downgraded_to_missing():
    def broken_reader(*args):
        raise ValueError("unrelated reader failure")

    with pytest.raises(ValueError, match="unrelated reader failure"):
        compute_reading(
            series=(load_definitions().by_id()["jp.10y"],),
            reader=broken_reader,
            rules=load_reading_rules(),
            rules_revision="synthetic",
            asof=DAY,
        )
