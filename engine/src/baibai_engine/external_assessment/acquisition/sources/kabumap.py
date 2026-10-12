"""Public Quants Research score display, not human analyst ratings."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

from baibai_engine.external_assessment.acquisition.sources import ParseError, compact, document
from baibai_engine.external_assessment.observations.models import QuantsScoreObservation
from baibai_engine.market.ticker import normalize_ticker

PARSER_VERSION = "kabumap-quants-1"
LABELS = {
    "成長性": "growth_score",
    "割安性": "undervaluation_score",
    "企業規模": "size_score",
    "テクニカル": "technical_score",
    "財務健全性": "financial_health_score",
    "市場トレンド": "market_trend_score",
}


def public_url(ticker: str) -> str:
    return (
        "https://jp.kabumap.com/servlets/kabumap/Action?SRC=basic%2Ffactor%2Fbase&codetext="
        + normalize_ticker(ticker)
    )


def _score(text: str, maximum: int) -> float | None:
    text = compact(text)
    if text in {"NA", "--", "-", f"NA/{maximum}", f"--/{maximum}"}:
        return None
    match = re.fullmatch(r"([0-9]+)/(" + str(maximum) + r")", text)
    if match is None or not 0 <= int(match[1]) <= maximum:
        raise ParseError("changed_quants_score")
    return float(match[1])


def parse_html(
    html: str, *, ticker: str, source_url: str, fetched_at_utc: datetime
) -> QuantsScoreObservation:
    ticker = normalize_ticker(ticker)
    if source_url != public_url(ticker):
        raise ParseError("source_identity")
    page = document(html)
    identities = page.find("span", id="stockCode")
    if (
        len(identities) != 1
        or re.fullmatch(r"\(東証[^:()]+:" + re.escape(ticker) + r"\)", compact(identities[0].text))
        is None
    ):
        raise ParseError("ticker_identity")
    dates = [
        match[1]
        for node in page.find("h2")
        if (
            match := re.fullmatch(
                r"クォンツスコア情報\((\d{4}-\d{2}-\d{2})更新\)", compact(node.text)
            )
        )
    ]
    if len(dates) != 1:
        raise ParseError("missing_quants_date")
    try:
        as_of = date.fromisoformat(dates[0])
    except ValueError:
        raise ParseError("missing_quants_date") from None
    tables = page.find("div", id="scoreTable")
    if len(tables) != 1:
        raise ParseError("missing_quants_scores")
    values: dict[str, float | None] = {}
    for row in tables[0].find("li"):
        if not row.has_class("scoreBase"):
            continue
        labels = [node for node in row.find("span") if node.has_class("scoreName")]
        cells = [node for node in row.find("span") if node.has_class("scoreFigure")]
        if len(labels) != 1 or len(cells) != 1:
            raise ParseError("changed_quants_score")
        label = unicodedata.normalize("NFKC", compact(labels[0].text))
        if label not in LABELS or LABELS[label] in values:
            raise ParseError("changed_quants_score")
        values[LABELS[label]] = _score(cells[0].text, 10)
    if set(values) != set(LABELS.values()):
        raise ParseError("missing_quants_scores")
    for identifier, name in (
        ("totalRow", "composite_score"),
        ("sectorRow", "industry_average_score"),
    ):
        rows = page.find("li", id=identifier)
        if len(rows) != 1:
            raise ParseError("missing_quants_scores")
        values[name] = _score(rows[0].text, 100)
    return QuantsScoreObservation.model_validate(
        {
            "ticker": ticker,
            "source_url": source_url,
            "fetched_at_utc": fetched_at_utc,
            "parser_version": PARSER_VERSION,
            "score_as_of_date": as_of,
            **values,
        }
    )
