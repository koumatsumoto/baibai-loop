"""Synthetic visible-table contract; no licensed report contents or live network."""

from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from baibai_engine.collection.equity_web.receipts import retain_response
from baibai_engine.external_assessment.analysis import analyze
from baibai_engine.external_assessment.models import AnalystForecastHistory, ObservationHistory
from baibai_engine.external_assessment.normalization import ParseError, minkabu
from baibai_engine.external_assessment.query import observed_tickers
from baibai_engine.external_assessment.reader import read_observations
from baibai_engine.external_assessment.rebuild import rebuild_attempts
from baibai_engine.external_assessment.store import ObservationConflictError, save_observations
from baibai_engine.storage.observations.schema import open_connection

NOW = datetime(2026, 10, 22, 7, tzinfo=UTC)


def html(ticker="7203", *, eps="4.5", with_forecast=True):
    forecast = """
    <div class="ly_content_wrapper"><h3>2027年の業績予想</h3>
    <table><tr><th></th><th colspan="4">証券アナリスト予想</th><th>会社予想</th></tr>
    <tr><th>3ヶ月前(2026/07/24)</th><th>1ヶ月前(2026/09/22)</th>
    <th>1週間前(2026/10/15)</th><th>最新(2026/10/22)</th><th>最新(2026/09/17)</th></tr>
    <tr><th>売上高</th><td>90</td><td>95</td><td>100</td><td>123</td><td>120</td></tr>
    <tr><th>当期利益</th><td>1</td><td>2</td><td>3</td><td>-4</td><td>5</td></tr>
    <tr><th>1株当り利益</th><td>1</td><td>2</td><td>3</td><td>EPS</td><td>6</td></tr>
    </table>※金額の単位：百万円（1株当りの項目のみ：円）</div>
    <div id="v-analyst-consensus"><table><tr><th>2027年3月期<br>アナリスト予想</th>
    <th>2027年3月期<br>会社予想</th></tr></table>
    <performance-trends-chart data-value="9999"></performance-trends-chart></div>
    """.replace("EPS", eps)
    return f"""
    <html><head><title>架空会社 ({ticker}) : アナリスト予想</title>
    <link rel="canonical" href="{minkabu.public_url(ticker)}"></head><body>
    <div><h2>目標株価</h2><span>99999円</span>総合予想・個人予想</div>
    <h2>アナリスト予想<br><span>(2026/10/22)</span></h2>
    <table><tr><th>強気買い</th><th>買い</th><th>中立</th><th>売り</th><th>強気売り</th></tr>
    <tr><td></td><td></td><td></td><td></td><td></td></tr>
    <tr><td>2人</td><td>1人</td><td>3人</td><td>0人</td><td>0人</td></tr></table>
    <table><tr><th></th><th>3ヶ月前</th><th>1ヶ月前</th><th>1週間前</th><th>最新</th></tr>
    <tr><th>評価</th><td>中立</td><td>中立</td><td>買い</td><td>買い</td></tr>
    <tr><th>予想株価</th><td>80</td><td>90</td><td>95</td><td>100</td></tr></table>
    「アナリストの予想株価」は、予想時点から1年後の株価を予想しています。
    {forecast if with_forecast else ""}
    </body></html>
    """


def parse(text=None, *, ticker="7203", at=NOW):
    return minkabu.parse_html(
        html(ticker) if text is None else text,
        ticker=ticker,
        source_url=minkabu.public_url(ticker),
        fetched_at_utc=at,
    )


def test_visible_analyst_fields_not_combined_target_or_chart_data():
    row = parse()
    assert row.target_price_yen == 100
    assert row.rating_label == "買い"
    assert (row.strong_buy_count, row.buy_count, row.neutral_count) == (2, 1, 3)
    assert row.target_horizon_months == 12
    assert row.forecast_period_label == "2027年3月期"
    assert row.analyst_revenue_million_yen == 123
    assert row.analyst_net_income_million_yen == -4
    assert row.analyst_eps_yen == 4.5
    assert row.company_eps_yen == 6
    assert row.origin is None and row.share_basis is None
    assert row.fetched_at_utc == NOW
    assert row.company_forecast_as_of_date.isoformat() == "2026-09-17"


@pytest.mark.parametrize("cell", ["---", "ー", "―", "--"])
def test_missing_is_not_zero(cell):
    assert parse(html(eps=cell)).analyst_eps_yen is None
    assert parse(html(eps="0")).analyst_eps_yen == 0


def test_rating_can_exist_without_earnings_forecast():
    row = parse(html(with_forecast=False))
    assert row.target_price_yen == 100
    assert row.forecast_period_label is None
    assert row.analyst_revenue_million_yen is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("7203", "130A"),
        ("強気売り</th>", "不明</th>"),
        ("2人", "2社"),
        ("百万円（1株当りの項目のみ：円）", "千円"),
        ("2027年3月期", "2028年3月期"),
        ("<td>123</td>", "<td>unexpected</td>"),
        ("(2026/10/22)", "(2026/13/22)"),
    ],
)
def test_changed_identity_labels_units_period_or_cells_are_not_normal_null(old, new):
    with pytest.raises(ParseError):
        parse(html().replace(old, new))


def test_alphanumeric_ticker():
    assert parse(ticker="130A").ticker == "130A"


def test_l1_replay_immutable_history_and_exact_cutoff(tmp_path):
    db = tmp_path / "market.sqlite"
    attempt = str(uuid4())
    with closing(open_connection(db)) as conn:
        retain_response(
            conn,
            attempt_id=attempt,
            source="minkabu",
            requested_ticker="7203",
            role="page",
            parent_receipt_id=None,
            requested_url=minkabu.public_url("7203"),
            received_at_utc=NOW,
            selected_encoding="utf-8",
            body=html().encode(),
        )
    assert rebuild_attempts(db, attempt_ids=[attempt]) == 1
    assert save_observations(db, [parse()]) == 0
    later = parse(html(eps="7"), at=NOW + timedelta(days=1))
    assert save_observations(db, [later]) == 1
    before = read_observations(db, "7203", NOW - timedelta(microseconds=1))
    assert before.minkabu.latest is None
    current = read_observations(db, "7203", NOW)
    assert current.minkabu.latest == parse()
    assert current.minkabu.previous is None
    assert analyze(current, evaluated_at=NOW).minkabu.latest == parse()
    history = read_observations(db, "7203", NOW + timedelta(days=1))
    assert history.minkabu.latest == later
    assert history.minkabu.previous == parse()
    assert observed_tickers(db, cutoff=NOW) == ["7203"]
    with pytest.raises(ObservationConflictError):
        save_observations(db, [parse(html(eps="9"))])


def test_analysis_rejects_future_or_different_security():
    for row in (parse(at=NOW + timedelta(seconds=1)), parse(ticker="130A")):
        history = ObservationHistory(
            ticker="7203",
            cutoff=NOW,
            minkabu=AnalystForecastHistory(latest=row),
        )
        with pytest.raises(ValueError, match="outside ticker/cutoff"):
            analyze(history, evaluated_at=NOW)
