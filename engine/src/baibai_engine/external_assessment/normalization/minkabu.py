"""Visible analyst tables only; never read the combined target or chart placeholders."""

from __future__ import annotations

import re
from datetime import date, datetime

from baibai_engine.collection.identifiers import normalize_ticker
from baibai_engine.external_assessment.normalization import (
    Element,
    ParseError,
    compact,
    document,
    number,
)
from baibai_engine.external_assessment.observations.models import AnalystForecastObservation

PARSER_VERSION = "minkabu-analyst-2"
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
        compact(child.text)
        for child in row.children
        if isinstance(child, Element) and child.tag in {"th", "td"}
    ]


def _number(text: str) -> float | None:
    return None if text in {"---", "ー"} else number(text, unit="rating")


def _date(text: str) -> date | None:
    match = re.search(r"\((\d{4})/(\d{2})/(\d{2})\)", text)
    if match is None:
        if text in {"最新", "最新(-)", "最新(---)", "最新(―)"}:
            return None
        raise ParseError("changed_analyst_forecast")
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        raise ParseError("changed_analyst_forecast") from None


def _table(page: Element, first_row: list[str]) -> list[list[str]]:
    matches = [
        [_cells(row) for row in table.find("tr")]
        for table in page.find("table")
        if table.find("tr") and _cells(table.find("tr")[0]) == first_row
    ]
    if len(matches) != 1:
        raise ParseError("changed_analyst_forecast")
    return matches[0]


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
            or boxes[0].tag != "span"
            or "href" in boxes[0].attrs
            or compact(boxes[0].text) != "アナリスト対象外"
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
    counts = _table(page, list(RATING_LABELS))
    if len(counts) != 3 or len(counts[-1]) != 5:
        raise ParseError("changed_analyst_forecast")
    values: dict[str, object] = {}
    for field, cell in zip(COUNT_FIELDS, counts[-1], strict=True):
        if re.fullmatch(r"[0-9]+人", cell) is None:
            raise ParseError("changed_analyst_forecast")
        values[field] = int(cell[:-1])
    history = _table(page, ["", "3ヶ月前", "1ヶ月前", "1週間前", "最新"])
    if (
        len(history) != 3
        or any(len(row) != 5 for row in history)
        or history[1][0] != "評価"
        or history[2][0] != "予想株価"
        or history[1][-1] not in RATING_LABELS
    ):
        raise ParseError("changed_analyst_forecast")
    values["rating_label"] = history[1][-1]
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
        rows = _table(section, ["", "証券アナリスト予想", "会社予想"])
        if (
            len(rows) != 5
            or len(rows[1]) != 5
            or [re.sub(r"\(.*\)", "", cell) for cell in rows[1]]
            != ["3ヶ月前", "1ヶ月前", "1週間前", "最新", "最新"]
        ):
            raise ParseError("changed_analyst_forecast")
        values["analyst_forecast_as_of_date"] = _date(rows[1][3])
        values["company_forecast_as_of_date"] = _date(rows[1][4])
        seen: set[str] = set()
        for row in rows[2:]:
            if len(row) != 6 or row[0] not in FORECAST_FIELDS or row[0] in seen:
                raise ParseError("changed_analyst_forecast")
            seen.add(row[0])
            field = FORECAST_FIELDS[row[0]]
            values[f"analyst_{field}"] = _number(row[4])
            values[f"company_{field}"] = _number(row[5])
        # Explicit absolute fiscal label from the visible annual table, not calendar-year inference.
        annual = page.find("div", id="v-analyst-consensus")
        labels = {
            compact(cell.text).removesuffix("アナリスト予想")
            for node in annual
            for cell in node.find("th")
            if re.fullmatch(r"[0-9]{4}年[0-9]{1,2}月期アナリスト予想", compact(cell.text))
        }
        if len(labels) == 1:
            label = labels.pop()
            year = re.match(r"[0-9]{4}", label)
            assert year is not None
            if not any(compact(h.text) == year[0] + "年の業績予想" for h in section.find("h3")):
                raise ParseError("changed_analyst_forecast")
            values["forecast_period_label"] = label
        elif labels:
            raise ParseError("changed_analyst_forecast")
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
