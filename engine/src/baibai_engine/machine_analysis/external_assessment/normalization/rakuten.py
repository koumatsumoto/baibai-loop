"""Follow the official forecast page's iframe and parse current rating only."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from baibai_engine.data_collection.identifiers import normalize_ticker
from baibai_engine.machine_analysis.external_assessment.normalization import (
    Element,
    ParseError,
    compact,
    document,
    number,
)
from baibai_engine.machine_analysis.external_assessment.observations.models import AnalystRatingObservation

PARSER_VERSION = "rakuten-rating-3"
IFRAME_BASE = "https://rakuten-sec.ifis.co.jp/index.php"
IFRAME_PARAMETER = "?action=tp4&sa=report_est&bcode="
_SCRIPT_ITEMS = re.compile(
    r"//[^\r\n]*|/\*[\s\S]*?\*/|<!--[^\r\n]*|-->[^\r\n]*"
    r"""|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'"""
    r"|`(?:\\.|[^`\\])*`"
    r"|(?P<constructor>\bnew\s+ChartOpConsClass\b)"
)
_CHART_CALL = re.compile(
    r"new\s+ChartOpConsClass\s*\(\s*(['\"])([^'\"\\\r\n]*)\1\s*,\s*"
    r"(['\"])([^'\"\\\r\n]*)\3\s*,\s*"
    r"(['\"])([^'\"\\\r\n]*)\5\s*,\s*"
    r"(['\"])([^'\"\\\r\n]*)\7\s*\)\s*;"
)


def _chart_identity(page: Element, ticker: str) -> bool:
    """Recognize only the captured widget's literal constructor, without JS execution."""
    calls = [
        _CHART_CALL.match(script.text, token.start())
        for script in page.find("script")
        for token in _SCRIPT_ITEMS.finditer(script.text)
        if token.group("constructor") is not None
    ]
    if not calls:
        return False
    expected = (
        "#chart_op_cons",
        "js/jsonp/chart_op_cons.json",
        f"https://img-sec.ifis.co.jp/graph/chart_op_cons/{ticker}.json",
        ticker,
    )
    if (
        len(calls) != 1
        or calls[0] is None
        or tuple(calls[0][index] for index in (2, 4, 6, 8)) != expected
        or len(page.find(id="chart_op_cons")) != 1
        or len(page.find("div", id="chart_op_cons")) != 1
    ):
        raise ParseError("ticker_identity")
    return True


def public_url(ticker: str) -> str:
    return (
        "https://www.rakuten-sec.co.jp/web/market/search/forecast.html?ric="
        + normalize_ticker(ticker)
        + ".T"
    )


def iframe_url(html: str, *, ticker: str, source_url: str) -> str:
    """Resolve only the verified official route, preserving getRIC()'s full value."""
    ticker = normalize_ticker(ticker)
    if source_url != public_url(ticker):
        raise ParseError("source_identity")
    page = document(html)
    identities = page.find("meta", property="og:url")
    if len(identities) != 1 or identities[0].attrs.get("content") != source_url:
        raise ParseError("ticker_identity")
    if len(page.find("iframe", id="ifr")) != 1:
        raise ParseError("missing_official_iframe")
    scripts = "\n".join(element.text for element in page.find("script"))
    base: list[str] = re.findall(r"\bvar\s+requestURL\s*=\s*['\"]([^'\"]+)['\"]", scripts)
    parameter: list[str] = re.findall(r"\bvar\s+requestPARAM\s*=\s*['\"]([^'\"]+)['\"]", scripts)
    if base != [IFRAME_BASE] or parameter != [IFRAME_PARAMETER]:
        raise ParseError("changed_official_iframe_route")
    if not re.search(r"requestURL\s*\+\s*requestPARAM\s*\+\s*getRIC\s*\(\s*\)", scripts):
        raise ParseError("changed_official_iframe_binding")
    return base[0] + parameter[0] + ticker + ".T"


def parse_html(
    html: str, *, ticker: str, source_url: str, fetched_at_utc: datetime
) -> AnalystRatingObservation:
    ticker = normalize_ticker(ticker)
    url = urlsplit(source_url)
    query = parse_qs(url.query)
    if (
        url.scheme != "https"
        or url.netloc != "rakuten-sec.ifis.co.jp"
        or url.path != "/index.php"
        or url.fragment
        or query
        not in (
            {"action": ["tp4"], "sa": ["report_est"], "bcode": [ticker + ".T"]},
            {"action": ["tp4"], "sa": ["report_est"], "bcode": [ticker]},
        )
    ):
        raise ParseError("source_identity")
    page = document(html)
    chart_identity = _chart_identity(page, ticker)
    # A company without its own rating can have only peer rows. The separately
    # verified chart widget provides explicit response identity in that case.
    comparison = [
        table
        for table in page.find("table")
        if [compact(th.text) for th in table.find("thead") for th in th.find("th")][:3]
        == ["コード", "会社名", "レーティング"]
    ]
    if len(comparison) != 1:
        # The public no-forecast shell has no response ticker. Its explicit
        # message explains the shell, but cannot establish a rating observation
        # for the caller's ticker or prove that the source excludes that ticker.
        if not comparison and any(
            compact(paragraph.text) == "この銘柄のアナリスト業績予測情報はありません。"
            for paragraph in page.find("p")
        ):
            raise ParseError("no_estimates_identity_unconfirmed")
        raise ParseError("missing_ticker_identity")
    bodies = comparison[0].find("tbody")
    rows = bodies[0].find("tr") if len(bodies) == 1 else []
    headers = rows[0].find("th") if rows else []
    if not chart_identity and (len(headers) != 1 or compact(headers[0].text) != ticker):
        raise ParseError("ticker_identity")
    sections = [
        element
        for element in page.find("div")
        if element.has_class("segment-s")
        and any(compact(heading.text) == "レーティング" for heading in element.find("h3"))
    ]
    if len(sections) != 1:
        raise ParseError("missing_rating_section")
    section = sections[0]
    tables = section.find("table")
    if len(tables) != 1:
        raise ParseError("changed_rating_table")
    heads = tables[0].find("thead")
    header_cells = heads[0].find("th") if len(heads) == 1 else []
    if [compact(th.text) for th in header_cells] != ["現在値", "1週間前", "1ヶ月前", "3ヶ月前"]:
        raise ParseError("changed_rating_headers")
    bodies = tables[0].find("tbody")
    rows = bodies[0].find("tr") if len(bodies) == 1 else []
    cells = rows[0].find("td") if len(rows) == 1 else []
    if len(cells) != 4 or cells[0].find("img"):
        raise ParseError("changed_rating_cell")
    rating = number(cells[0].text, unit="rating")
    # Neither a rating scale nor a rating-specific count is present in the
    # verified public HTML. The forecast-section count is a different metric.
    return AnalystRatingObservation.model_validate(
        {
            "ticker": ticker,
            "distributor": "rakuten",
            "origin": "IFIS",
            "source_url": source_url,
            "fetched_at_utc": fetched_at_utc,
            "parser_version": PARSER_VERSION,
            "rating": rating,
        }
    )
