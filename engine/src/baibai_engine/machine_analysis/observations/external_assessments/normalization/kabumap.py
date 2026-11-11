"""Public Quants Research score display, not human analyst ratings."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

from baibai_engine.machine_analysis.observations.external_assessments.models import (
    QuantsScoreObservation,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization import (
    Element,
    ParseError,
    compact,
    document,
    numeric_text,
)
from baibai_engine.primitives.stocks import normalize_ticker

PARSER_VERSION = "kabumap-quants-4"
_RETAINED_PARSER_VERSION = "kabumap-quants-3"
_NOT_COMPUTED_HEADING = "クォンツスコア情報（まだ計算されていない銘柄です）"
_NOT_COMPUTED_MESSAGE = "クォンツスコアはまだ計算されていません"
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
    text = text.strip()
    if text in {"NA", "--", "-"}:
        return None
    match = re.fullmatch(r"(NA|--|[0-9]+)\s*/\s*" + str(maximum), text)
    if match is None:
        raise ParseError("changed_quants_score")
    if match[1] in {"NA", "--"}:
        return None
    if not 0 <= int(match[1]) <= maximum:
        raise ParseError("changed_quants_score")
    return float(match[1])


def _visible_text(node: Element) -> str:
    if node.tag in {"head", "script", "style", "template"}:
        return ""
    return compact(
        "".join(
            child if isinstance(child, str) else _visible_text(child) for child in node.children
        )
    )


def _score_date(page: Element, parser_version: str) -> date | None:
    if parser_version == _RETAINED_PARSER_VERSION:
        # 保存stageの原本検証では、そのときの受理条件とversionを維持する。
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
            return date.fromisoformat(dates[0])
        except ValueError:
            raise ParseError("missing_quants_date") from None
    headings = [
        compact(node.text)
        for node in page.find("h2")
        if compact(node.text).startswith("クォンツスコア情報")
    ]
    if len(headings) != 1:
        raise ParseError("missing_quants_date")
    not_computed = headings[0] == _NOT_COMPUTED_HEADING
    if not_computed != (_NOT_COMPUTED_MESSAGE in _visible_text(page)):
        raise ParseError("missing_quants_date")
    as_of = None
    if not not_computed:
        match = re.fullmatch(r"クォンツスコア情報\((\d{4}-\d{2}-\d{2})更新\)", headings[0])
        if match is None:
            raise ParseError("missing_quants_date")
        try:
            as_of = date.fromisoformat(match[1])
        except ValueError:
            raise ParseError("missing_quants_date") from None
    return as_of


def parse_html(
    html: str,
    *,
    ticker: str,
    source_url: str,
    fetched_at_utc: datetime,
    parser_version: str = PARSER_VERSION,
) -> QuantsScoreObservation:
    if parser_version not in {PARSER_VERSION, _RETAINED_PARSER_VERSION}:
        raise ParseError("unsupported_parser_version")
    ticker = normalize_ticker(ticker)
    if source_url != public_url(ticker):
        raise ParseError("source_identity")
    page = document(html)
    identities = page.find("span", id="stockCode")
    if (
        len(identities) != 1
        or re.fullmatch(r"\(東証[^:()]*:" + re.escape(ticker) + r"\)", compact(identities[0].text))
        is None
    ):
        raise ParseError("ticker_identity")
    as_of = _score_date(page, parser_version)
    not_computed = as_of is None
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
        values[LABELS[label]] = _score(numeric_text(cells[0]), 10)
    if set(values) != set(LABELS.values()):
        raise ParseError("missing_quants_scores")
    for identifier, name in (
        ("totalRow", "composite_score"),
        ("sectorRow", "industry_average_score"),
    ):
        rows = page.find("li", id=identifier)
        if len(rows) != 1 or (
            parser_version == PARSER_VERSION and not tables[0].find("li", id=identifier)
        ):
            raise ParseError("missing_quants_scores")
        values[name] = _score(numeric_text(rows[0]), 100)
    if not_computed and any(value is not None for value in values.values()):
        raise ParseError("changed_quants_score")
    return QuantsScoreObservation.model_validate(
        {
            "ticker": ticker,
            "source_url": source_url,
            "fetched_at_utc": fetched_at_utc,
            "parser_version": parser_version,
            "availability": "not_computed" if not_computed else "available",
            "score_as_of_date": as_of,
            **values,
        }
    )
