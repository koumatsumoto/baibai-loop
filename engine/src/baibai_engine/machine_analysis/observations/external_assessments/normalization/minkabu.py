"""Visible analyst tables only; never read the combined target or chart placeholders."""

from __future__ import annotations

import re
from datetime import date, datetime

from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.machine_analysis.observations.external_assessments.normalization import (
    Element,
    ParseError,
    compact,
    document,
    number,
    numeric_text,
)
from baibai_engine.machine_analysis.observations.external_assessments.models import (
    AnalystForecastObservation,
)

PARSER_VERSION = "minkabu-analyst-6"
RATING_LABELS = ("強気買い", "買い", "中立", "売り", "強気売り")
COUNT_FIELDS = ("strong_buy_count", "buy_count", "neutral_count", "sell_count", "strong_sell_count")
FORECAST_FIELDS = {
    "売上高": "revenue_million_yen",
    "当期利益": "net_income_million_yen",
    "1株当り利益": "eps_yen",
}


def public_url(ticker: str) -> str:
    return f"https://minkabu.jp/stock/{normalize_ticker(ticker)}/analyst_consensus"


def profile_url(ticker: str) -> str:
    return f"https://minkabu.jp/stock/{normalize_ticker(ticker)}"


def _cells(row: Element) -> list[str]:
    return [
        numeric_text(child).strip()
        for child in row.children
        if isinstance(child, Element) and child.tag in {"th", "td"}
    ]


def _number(text: str) -> float | None:
    return None if text in {"", "---", "ー"} else number(text, unit="rating")


def _date(text: str) -> date | None:
    text = compact(text)
    match = re.search(r"\((\d{4})/(\d{2})/(\d{2})\)", text)
    if match is None:
        if text in {"最新", "最新(-)", "最新(---)", "最新(―)"}:
            return None
        raise ParseError("changed_analyst_forecast")
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        raise ParseError("changed_analyst_forecast") from None


def _table(page: Element, first_row: list[str]) -> Element:
    matches = [
        table
        for table in page.find("table")
        if table.find("tr") and [compact(cell) for cell in _cells(table.find("tr")[0])] == first_row
    ]
    if len(matches) != 1:
        raise ParseError("changed_analyst_forecast")
    return matches[0]


def _span(cell: Element, attribute: str) -> int:
    value = cell.attrs.get(attribute, "1").strip()
    if re.fullmatch(r"[0-9]+", value) is None or int(value) < 1:
        raise ParseError("changed_analyst_forecast")
    return int(value)


def _annual_forecast(page: Element, *, include_values: bool) -> dict[str, object]:
    """Use visible annual forecasts only; absence of comparison dates remains unknown."""
    sections = page.find("div", id="v-analyst-consensus")
    if not sections:
        return {}
    if len(sections) != 1:
        raise ParseError("changed_analyst_forecast")
    tables = sections[0].find("table")
    if len(tables) != 1:
        raise ParseError("changed_analyst_forecast")
    rows = [_cells(row) for row in tables[0].find("tr")]
    if not rows:
        raise ParseError("changed_analyst_forecast")
    columns: dict[str, int] = {}
    periods: set[str] = set()
    for index, label in enumerate(rows[0]):
        match = re.fullmatch(
            r"([0-9]{4}年([0-9]{1,2})月期)(アナリスト予想|会社予想)", compact(label)
        )
        if match is None:
            continue
        if not 1 <= int(match[2]) <= 12 or match[3] in columns:
            raise ParseError("changed_analyst_forecast")
        periods.add(match[1])
        columns[match[3]] = index
    if len(periods) != 1 or set(columns) != {"アナリスト予想", "会社予想"}:
        raise ParseError("changed_analyst_forecast")
    values: dict[str, object] = {"forecast_period_label": periods.pop()}
    if not include_values:
        return values
    if "単位：百万円" not in compact(sections[0].text):
        raise ParseError("changed_analyst_forecast")
    for label, metric in (("売上高", "revenue_million_yen"), ("純利益", "net_income_million_yen")):
        selected = [row for row in rows[1:] if row and compact(row[0]) == label]
        if len(selected) != 1 or len(selected[0]) != len(rows[0]):
            raise ParseError("changed_analyst_forecast")
        for source, field in (("アナリスト予想", "analyst"), ("会社予想", "company")):
            values[f"{field}_{metric}"] = _number(selected[0][columns[source]])
    return values


def parse_html(
    html: str, *, ticker: str, source_url: str, fetched_at_utc: datetime
) -> AnalystForecastObservation:
    ticker = normalize_ticker(ticker)
    if source_url not in {public_url(ticker), profile_url(ticker)}:
        raise ParseError("source_identity")
    page = document(html)
    canonical = page.find("link", rel="canonical")
    titles = page.find("title")
    if (
        len(canonical) != 1
        or canonical[0].attrs.get("href") != source_url
        or len(titles) != 1
        or f"({ticker})" not in compact(titles[0].text)
    ):
        raise ParseError("ticker_identity")
    if source_url == profile_url(ticker):
        boxes = [
            node
            for node in page.find()
            if node.has_class("md_box")
            and [compact(label.text) for label in node.find("p", **{"class": "label"})]
            == ["アナリスト"]
        ]
        if (
            len(boxes) != 1
            or compact(boxes[0].text) != "アナリスト対象外"
            or not (
                (boxes[0].tag == "span" and "href" not in boxes[0].attrs)
                or (
                    boxes[0].tag == "a"
                    and boxes[0].attrs.get("href") == f"/stock/{ticker}/analyst_consensus"
                )
            )
        ):
            raise ParseError("unconfirmed_no_analyst_coverage")
        return AnalystForecastObservation.model_validate(
            {
                "ticker": ticker,
                "fetched_at_utc": fetched_at_utc,
                "source_url": source_url,
                "parser_version": PARSER_VERSION,
                "availability": "not_covered",
            }
        )
    headings = [
        compact(node.text)
        for node in page.find("h2")
        if compact(node.text).startswith("アナリスト予想(")
    ]
    if len(headings) != 1:
        raise ParseError("missing_analyst_forecast")
    as_of = _date(headings[0])
    if as_of is None:
        raise ParseError("missing_analyst_forecast")
    counts = [_cells(row) for row in _table(page, list(RATING_LABELS)).find("tr")]
    if len(counts) != 3 or len(counts[-1]) != 5:
        raise ParseError("changed_analyst_forecast")
    values: dict[str, object] = {}
    for field, cell in zip(COUNT_FIELDS, counts[-1], strict=True):
        match = re.fullmatch(r"([0-9]+)\s*人", cell)
        if match is None:
            raise ParseError("changed_analyst_forecast")
        values[field] = int(match[1])
    # Read the identified analyst card even when no historical target exists.
    cards = [
        node
        for node in page.find("div")
        if node.has_class("md_card_ti")
        and any(compact(h.text).startswith("アナリスト予想(") for h in node.find("h2"))
    ]
    if len(cards) != 1:
        raise ParseError("changed_analyst_forecast")
    labels = [compact(n.text) for n in cards[0].find("a") if n.has_class("md_picksPlate")]
    prices = [numeric_text(n).strip() for n in cards[0].find("span") if n.has_class("fsxxl")]
    if len(labels) != 1 or labels[0] not in RATING_LABELS or len(prices) != 1:
        raise ParseError("changed_analyst_forecast")
    values["rating_label"] = labels[0]
    values["target_price_yen"] = _number(prices[0])
    # Keep the existing latest-table value when present. The visible analyst card
    # and table can differ by one yen; do not infer their hidden rounding policy.
    histories = [
        [_cells(row) for row in table.find("tr")]
        for table in page.find("table")
        if table.find("tr")
        and [compact(cell) for cell in _cells(table.find("tr")[0])]
        == ["", "3ヶ月前", "1ヶ月前", "1週間前", "最新"]
    ]
    if len(histories) > 1:
        raise ParseError("changed_analyst_forecast")
    if histories:
        history = histories[0]
        if (
            len(history) != 3
            or any(len(row) != 5 for row in history)
            or compact(history[1][0]) != "評価"
            or compact(history[2][0]) != "予想株価"
            or compact(history[1][-1]) not in RATING_LABELS
        ):
            raise ParseError("changed_analyst_forecast")
        values["rating_label"] = compact(history[1][-1])
        values["target_price_yen"] = _number(history[2][-1])
    if "「アナリストの予想株価」は、予想時点から1年後の株価を予想しています。" in compact(
        page.text
    ):
        values["target_horizon_months"] = 12

    sections = [
        node
        for node in page.find("div")
        if node.has_class("ly_content_wrapper")
        and any(re.fullmatch(r"[0-9]{4}年の業績予想", compact(h.text)) for h in node.find("h3"))
    ]
    if len(sections) > 1:
        raise ParseError("changed_analyst_forecast")
    if sections:
        section = sections[0]
        if "※金額の単位：百万円（1株当りの項目のみ：円）" not in compact(section.text):
            raise ParseError("changed_analyst_forecast")
        table = _table(section, ["", "証券アナリスト予想", "会社予想"])
        headers = [
            node
            for node in table.find("tr")[0].children
            if isinstance(node, Element) and node.tag in {"th", "td"}
        ]
        if [(_span(header, "colspan"), _span(header, "rowspan")) for header in headers] != [
            (1, 2),
            (4, 1),
            (1, 1),
        ] or any(
            (_span(cell, "colspan"), _span(cell, "rowspan")) != (1, 1)
            for row in table.find("tr")[1:]
            for cell in row.children
            if isinstance(cell, Element) and cell.tag in {"th", "td"}
        ):
            raise ParseError("changed_analyst_forecast")
        rows = [_cells(row) for row in table.find("tr")]
        if (
            len(rows) != 5
            or len(rows[1]) != 5
            or [re.sub(r"\(.*\)", "", compact(cell)) for cell in rows[1]]
            != ["3ヶ月前", "1ヶ月前", "1週間前", "最新", "最新"]
        ):
            raise ParseError("changed_analyst_forecast")
        values["analyst_forecast_as_of_date"] = _date(rows[1][3])
        values["company_forecast_as_of_date"] = _date(rows[1][4])
        seen: set[str] = set()
        for row in rows[2:]:
            label = compact(row[0]) if row else ""
            if len(row) != 6 or label not in FORECAST_FIELDS or label in seen:
                raise ParseError("changed_analyst_forecast")
            seen.add(label)
            field = FORECAST_FIELDS[label]
            values[f"analyst_{field}"] = _number(row[4])
            values[f"company_{field}"] = _number(row[5])
    annual = _annual_forecast(page, include_values=not sections)
    values.update(annual)
    if sections and "forecast_period_label" in annual:
        year = str(annual["forecast_period_label"])[:4]
        if not any(compact(h.text) == year + "年の業績予想" for h in sections[0].find("h3")):
            raise ParseError("conflicting_forecast_period")
    return AnalystForecastObservation.model_validate(
        {
            "ticker": ticker,
            "fetched_at_utc": fetched_at_utc,
            "source_url": source_url,
            "parser_version": PARSER_VERSION,
            "display_as_of_date": as_of,
            **values,
        }
    )
