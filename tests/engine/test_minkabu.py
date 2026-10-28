"""Synthetic visible-table contract; no licensed report contents or live network."""

from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from baibai_engine.data_collection.equity_web.receipts import retain_response
from baibai_engine.machine_analysis.models.external_assessment.analysis import analyze
from baibai_engine.machine_analysis.models.external_assessment.models import (
    AnalystForecastHistory,
    ObservationHistory,
)
from baibai_engine.machine_analysis.models.external_assessment.normalization import ParseError, minkabu
from baibai_engine.machine_analysis.models.external_assessment.reader import read_observations
from baibai_engine.machine_analysis.models.external_assessment.rebuild import rebuild_attempts
from baibai_engine.machine_analysis.models.external_assessment.store import (
    ObservationConflictError,
    save_observations,
)
from baibai_engine.storage.observations.schema import open_connection

NOW = datetime(2026, 10, 22, 7, tzinfo=UTC)


def html(ticker="7203", *, eps="4.5", with_forecast=True):
    forecast = """
    <div class="ly_content_wrapper"><h3>2027年の業績予想</h3>
    <table><tr><th rowspan="2"></th>
    <th colspan="4">証券アナリスト予想</th><th>会社予想</th></tr>
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
    <div class="md_card_ti theme_buy">
    <h2>アナリスト予想<br><span>(2026/10/22)</span></h2>
    <a class="md_picksPlate">買い</a><span class="fsxxl">100</span></div>
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
    assert row.origin is None
    assert row.share_basis is None
    assert row.fetched_at_utc == NOW
    assert row.company_forecast_as_of_date.isoformat() == "2026-09-17"


@pytest.mark.parametrize("cell", ["", " \n\t", "---", "ー", "―", "--"])
def test_missing_is_not_zero(cell):
    row = parse(html(eps=cell))
    assert row.analyst_eps_yen is None
    assert row.rating_label == "買い"
    assert row.target_price_yen == 100
    assert row.analyst_revenue_million_yen == 123
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


def no_coverage_html(ticker="130A", *, linked=False):
    tag = "a" if linked else "span"
    href = f' href="/stock/{ticker}/analyst_consensus"' if linked else ""
    return f'''<html><head><title>合成会社 ({ticker}) : 株価</title>
    <link rel="canonical" href="{minkabu.profile_url(ticker)}"></head><body>
    <span class="md_box"><p class="label">個人予想</p><span>対象外</span></span>
    <{tag} class="ly_col md_box"{href}><p class="label">アナリスト</p>
    <span class="md_picksPlate theme_none">対象外</span></{tag}></body></html>'''


@pytest.mark.parametrize("linked", [False, True])
def test_explicit_identified_no_coverage_is_not_zero_or_an_http_error(tmp_path, linked):
    from baibai_engine.data_collection.equity_web.models import EquityWebSourceResponse
    from baibai_engine.machine_analysis.models.external_assessment.normalization.decode import (
        decode_attempt,
    )

    response = EquityWebSourceResponse(
        str(uuid4()),
        str(uuid4()),
        "minkabu",
        "130A",
        "page",
        None,
        minkabu.profile_url("130A"),
        NOW,
        "utf-8",
        no_coverage_html(linked=linked).encode(),
    )
    row = decode_attempt([response])
    assert row.availability == "not_covered"
    assert row.display_as_of_date is None
    assert row.strong_buy_count is None
    assert row.target_price_yen is None
    db = tmp_path / "market.sqlite"
    assert save_observations(db, [row]) == 1
    assert read_observations(db, "130A", NOW).minkabu.latest == row


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("130A", "7203"),
        ("アナリスト</p>", "個人予想</p>"),
        ("対象外", "---"),
        ('class="ly_col md_box"', 'class="unrelated"'),
        ('<span class="ly_col md_box">', '<span href="/unexpected" class="ly_col md_box">'),
    ],
)
def test_404_or_incidental_missing_text_never_proves_no_coverage(old, new):
    with pytest.raises(ParseError):
        minkabu.parse_html(
            no_coverage_html().replace(old, new),
            ticker="130A",
            source_url=minkabu.profile_url("130A"),
            fetched_at_utc=NOW,
        )


@pytest.mark.parametrize(
    "href",
    [
        "/stock/7203/analyst_consensus",
        "https://other.example/stock/130A/analyst_consensus",
        "//other.example/stock/130A/analyst_consensus",
        "https://minkabu.jp/stock/130A/analyst_consensus",
        "/stock/130A/analyst_consensus?other=1",
        "/stock/130A/analyst_consensus#other",
        "/stock/130A",
        "",
    ],
)
def test_linked_no_coverage_requires_exact_same_ticker_relative_link(href):
    text = no_coverage_html(linked=True).replace(
        'href="/stock/130A/analyst_consensus"', f'href="{href}"'
    )
    with pytest.raises(ParseError, match="unconfirmed_no_analyst_coverage"):
        minkabu.parse_html(
            text, ticker="130A", source_url=minkabu.profile_url("130A"), fetched_at_utc=NOW
        )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("130A", "7203"),
        ("アナリスト</p>", "個人予想</p>"),
        ("対象外", "対象外（推定）"),
        ('class="ly_col md_box"', 'class="unrelated"'),
        (' href="/stock/130A/analyst_consensus"', ""),
        ('<a class="ly_col md_box"', '<div class="ly_col md_box"'),
    ],
)
def test_linked_no_coverage_keeps_identity_card_and_exact_text_checks(old, new):
    with pytest.raises(ParseError):
        minkabu.parse_html(
            no_coverage_html(linked=True).replace(old, new),
            ticker="130A",
            source_url=minkabu.profile_url("130A"),
            fetched_at_utc=NOW,
        )


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("other_text", ["対象外", "買い"])
def test_duplicate_or_conflicting_analyst_cards_do_not_prove_no_coverage(linked, other_text):
    other = f'<span class="md_box"><p class="label">アナリスト</p>{other_text}</span>'
    text = no_coverage_html(linked=linked).replace("</body>", other + "</body>")
    with pytest.raises(ParseError, match="unconfirmed_no_analyst_coverage"):
        minkabu.parse_html(
            text, ticker="130A", source_url=minkabu.profile_url("130A"), fetched_at_utc=NOW
        )


def test_no_coverage_cannot_carry_values_and_available_requires_display_date():
    from pydantic import ValidationError

    from baibai_engine.machine_analysis.models.external_assessment.observations.models import (
        AnalystForecastObservation,
    )

    base = dict(
        ticker="130A",
        fetched_at_utc=NOW,
        source_url=minkabu.profile_url("130A"),
        parser_version="synthetic",
    )
    with pytest.raises(ValidationError):
        AnalystForecastObservation(**base)
    with pytest.raises(ValidationError):
        AnalystForecastObservation(**base, availability="not_covered", strong_buy_count=0)


def test_later_no_coverage_does_not_leave_previous_forecast_as_latest(tmp_path):
    db = tmp_path / "market.sqlite"
    earlier = parse(ticker="130A")
    later = minkabu.parse_html(
        no_coverage_html(),
        ticker="130A",
        source_url=minkabu.profile_url("130A"),
        fetched_at_utc=NOW + timedelta(days=1),
    )
    save_observations(db, [earlier, later])
    history = read_observations(db, "130A", NOW + timedelta(days=1)).minkabu
    assert history.latest == later
    assert history.previous == earlier
    assert read_observations(db, "130A", NOW).minkabu.latest == earlier


def current_card_only_html(ticker="7203"):
    page = html(ticker, with_forecast=False)
    start = page.index("<table><tr><th></th><th>3ヶ月前")
    end = page.index("</table>", start) + len("</table>")
    page = page[:start] + page[end:]
    page = page.replace('<span class="fsxxl">100</span>', '<span class="fsxxl">---</span>')
    return page.replace(
        "</body>",
        """
    <div id="v-analyst-consensus"><table>
    <tr><th></th><th>2026年3月期</th><th>2027年3月期アナリスト予想</th><th>2027年3月期会社予想</th></tr>
    <tr><th>売上高</th><td>80</td><td>123</td><td>120</td></tr>
    <tr><th>純利益</th><td>1</td><td>-4</td><td>5</td></tr>
    <tr><th>営業利益</th><td>2</td><td>---</td><td>6</td></tr>
    <tr><th>経常利益</th><td>3</td><td>---</td><td>7</td></tr>
    </table><p>単位：百万円</p>
    <performance-trends-chart data-value="0"></performance-trends-chart></div></body>""",
    )


def test_current_rating_without_target_or_comparison_table_keeps_visible_annual_forecasts():
    row = parse(current_card_only_html())
    assert row.rating_label == "買い"
    assert row.target_price_yen is None
    assert row.availability == "available"
    assert row.forecast_period_label == "2027年3月期"
    assert row.analyst_revenue_million_yen == 123
    assert row.company_revenue_million_yen == 120
    assert row.analyst_net_income_million_yen == -4
    assert row.company_net_income_million_yen == 5
    assert row.analyst_eps_yen is None
    assert row.company_eps_yen is None
    assert row.analyst_forecast_as_of_date is None
    assert row.company_forecast_as_of_date is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('class="md_card_ti theme_buy"', 'class="unrelated"'),
        ('class="fsxxl"', 'class="unrelated"'),
        ("単位：百万円", "単位：千円"),
        ("2027年3月期会社予想", "2028年3月期会社予想"),
        ("2027年3月期", "2027年13月期"),
        ("<td>123</td>", "<td>unrecognized</td>"),
        ("<th>純利益</th>", "<th>unknown</th>"),
    ],
)
def test_current_card_and_annual_values_require_explicit_identity_units_and_cells(old, new):
    with pytest.raises(ParseError):
        parse(current_card_only_html().replace(old, new))


def test_visible_history_priority_does_not_infer_hidden_rounding_from_card():
    text = html().replace('<span class="fsxxl">100</span>', '<span class="fsxxl">99</span>')
    assert parse(text).target_price_yen == 100


def test_conflicting_displayed_forecast_year_is_not_silently_reconciled():
    from baibai_engine.data_collection.external_assessment.collection import PARSE_FAILURE_REASONS

    # Equal figures do not establish which of two conflicting period labels is true.
    text = html().replace("2027年3月期", "2026年12月期")
    with pytest.raises(ParseError, match=r"^conflicting_forecast_period$"):
        parse(text)
    assert "conflicting_forecast_period" in PARSE_FAILURE_REASONS


@pytest.mark.parametrize(
    "text",
    [
        html(eps="1 2"),
        html(eps="1<br>2"),
        html(eps="<span>1 </span><span>2</span>"),
        html(eps="1, 234"),
        html(eps="- 4"),
        html().replace("2人", "2 1人"),
        html().replace("<td>95</td><td>100</td></tr>", "<td>95</td><td>1 2</td></tr>"),
        current_card_only_html().replace(
            '<span class="fsxxl">---</span>', '<span class="fsxxl">1 2</span>'
        ),
        current_card_only_html().replace("<td>123</td>", "<td>1 2</td>"),
    ],
    ids=[
        "eps",
        "line-break",
        "span-whitespace",
        "comma",
        "sign",
        "count",
        "history-target",
        "card-target",
        "annual-revenue",
    ],
)
def test_numeric_internal_whitespace_is_not_removed(text):
    with pytest.raises(ParseError):
        parse(text)


def test_numeric_outer_whitespace_and_adjacent_spans_keep_displayed_values():
    text = (
        html(eps="<br> \n<span>1</span><span>2</span>\t <br>")
        .replace("<td>2人</td>", "<td> \n<span>2</span> 人\t </td>")
        .replace("証券アナリスト予想", "証券<br>アナリスト予想")
        .replace("<th>売上高</th>", "<th>売 上高</th>")
        .replace("<td>買い</td>", "<td>買 い</td>")
        .replace("最新(2026/10/22)", "最新\n(2026/10/22)")
    )
    row = parse(text)
    assert row.analyst_eps_yen == 12
    assert row.strong_buy_count == 2
    assert row.rating_label == "買い"
    assert row.analyst_forecast_as_of_date.isoformat() == "2026-10-22"
    assert row.company_forecast_as_of_date.isoformat() == "2026-09-17"
    text = (
        current_card_only_html()
        .replace(
            '<span class="fsxxl">---</span>',
            '<span class="fsxxl"> \n<span>1</span><span>00</span>\t </span>',
        )
        .replace("<td>123</td>", "<td> \n<span>1</span><span>23</span>\t </td>")
    )
    row = parse(text)
    assert row.target_price_yen == 100
    assert row.analyst_revenue_million_yen == 123
    assert row.forecast_period_label == "2027年3月期"


@pytest.mark.parametrize(
    ("analyst_span", "company_span"),
    [
        ("3", "2"),
        ("5", "1"),
        ("4", "2"),
        ("0", "1"),
        ("-4", "1"),
        ("", "1"),
        ("four", "1"),
        ("４", "1"),
        ("0 4", "1"),
    ],
)
def test_detailed_forecast_requires_analyst_and_company_column_groups(analyst_span, company_span):
    text = html().replace(
        'colspan="4">証券アナリスト予想</th><th>会社予想',
        f'colspan="{analyst_span}">証券アナリスト予想</th><th colspan="{company_span}">会社予想',
    )
    with pytest.raises(ParseError, match="changed_analyst_forecast"):
        parse(text)


@pytest.mark.parametrize(
    ("analyst_span", "blank_rowspan"),
    [("4", "2"), ("04", "02"), (" 4 ", " 2 "), ("\n04\t", "\n02\t")],
)
def test_detailed_forecast_equivalent_spans_preserve_values_and_dates(analyst_span, blank_rowspan):
    text = (
        html()
        .replace('colspan="4"', f'colspan="{analyst_span}" rowspan="01"')
        .replace('rowspan="2"', f'colspan="01" rowspan="{blank_rowspan}"', 1)
        .replace("<th>会社予想</th>", '<th colspan="01" rowspan="01">会社予想</th>')
        .replace("<th>3ヶ月前(", '<th colspan="01" rowspan="01">3ヶ月前(')
        .replace("<td>123</td>", '<td colspan="01" rowspan="01">123</td>')
    )
    row = parse(text)
    assert row.analyst_revenue_million_yen == 123
    assert row.company_revenue_million_yen == 120
    assert row.analyst_forecast_as_of_date.isoformat() == "2026-10-22"
    assert row.company_forecast_as_of_date.isoformat() == "2026-09-17"


@pytest.mark.parametrize("rowspan", [None, "1", "3", "0", "-2", "", "two", "２", "0 2"])
def test_detailed_forecast_blank_header_requires_two_rows(rowspan):
    attribute = "" if rowspan is None else f'rowspan="{rowspan}"'
    text = html().replace('rowspan="2"', attribute, 1)
    with pytest.raises(ParseError, match="changed_analyst_forecast"):
        parse(text)


def test_detailed_forecast_analyst_group_cannot_span_date_row():
    text = html().replace('colspan="4"', 'colspan="4" rowspan="2"')
    with pytest.raises(ParseError, match="changed_analyst_forecast"):
        parse(text)


@pytest.mark.parametrize("attribute", ["colspan", "rowspan"])
@pytest.mark.parametrize(
    "cell",
    ["<th>会社予想</th>", "<th>3ヶ月前(2026/07/24)</th>", "<th>売上高</th>", "<td>123</td>"],
)
def test_detailed_forecast_cells_cannot_span_rows_or_columns(cell, attribute):
    text = html().replace(cell, cell.replace(">", f' {attribute}="2">', 1))
    with pytest.raises(ParseError, match="changed_analyst_forecast"):
        parse(text)
