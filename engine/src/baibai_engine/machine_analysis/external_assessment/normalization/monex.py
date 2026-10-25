"""Parse the ordinary public Scouter Lite theory-price table."""

from __future__ import annotations

from datetime import datetime

from baibai_engine.data_collection.identifiers import normalize_ticker
from baibai_engine.machine_analysis.external_assessment.normalization import (
    ParseError,
    compact,
    document,
    number,
)
from baibai_engine.machine_analysis.external_assessment.observations.models import (
    ModelValuationObservation,
)

PARSER_VERSION = "monex-theory-3"


def public_url(ticker: str) -> str:
    return f"https://scouter.monex.co.jp/report/theoryDps/{normalize_ticker(ticker)}"


def parse_html(
    html: str, *, ticker: str, source_url: str, fetched_at_utc: datetime
) -> ModelValuationObservation:
    """Replay captured HTML with its original receipt time; never infer a judgment."""
    ticker = normalize_ticker(ticker)
    if source_url != public_url(ticker):
        raise ParseError("source_identity")
    page = document(html)
    identities = page.find("input", id="stockCd")
    if len(identities) != 1 or identities[0].attrs.get("value") != ticker:
        raise ParseError("ticker_identity")
    sections = [
        element for element in page.find("div") if element.has_class("theory_summary_inner")
    ]
    if len(sections) != 1:
        raise ParseError("missing_theory_table")
    expected = {
        "目標株価コンセンサス": "consensus_target",
        "PER基準会社予想": "per_company_forecast",
        "PER基準アナリスト予想": "per_consensus",
        "PBR基準": "pbr",
    }
    values: dict[str, float | None] = {}
    for table in sections[0].find("table"):
        headers = table.find("th")
        if len(headers) != 1:
            raise ParseError("changed_theory_table")
        label = compact(headers[0].text)
        if label not in expected:
            if label.startswith("株価"):
                continue
            raise ParseError("changed_theory_label")
        cells = table.find("td")
        name = expected[label]
        if len(cells) != 1 or name in values or cells[0].find("img"):
            raise ParseError("changed_theory_cell")
        values[name] = number(cells[0].text, unit="yen")
    if set(values) != set(expected.values()):
        raise ParseError("incomplete_theory_table")
    return ModelValuationObservation.model_validate(
        {
            "ticker": ticker,
            "distributor": "monex",
            "origin": "IFIS",
            "source_url": source_url,
            "fetched_at_utc": fetched_at_utc,
            "parser_version": PARSER_VERSION,
            "currency": "JPY",
            "unit": "JPY/share",
            "consensus_target_origin": "IFIS",
            "per_company_forecast_origin": "Monex Scouter model",
            "per_consensus_origin": "Monex Scouter model",
            "pbr_origin": "Monex Scouter model",
            **values,
        }
    )
