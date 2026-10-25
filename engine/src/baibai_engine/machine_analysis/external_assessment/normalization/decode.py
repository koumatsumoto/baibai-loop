"""Decode one explicitly retained public-page attempt without acquiring data."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from baibai_engine.data_collection.equity_web.models import EquityWebSourceResponse, Source
from baibai_engine.machine_analysis.external_assessment.normalization import (
    ParseError,
    kabumap,
    minkabu,
    monex,
    rakuten,
)
from baibai_engine.machine_analysis.external_assessment.observations.models import Observation


def parse_source_html(
    html: str, *, source: Source, ticker: str, source_url: str, fetched_at_utc: datetime
) -> Observation:
    """Select the source parser without acquiring pages or rebinding receipts."""
    parser = (
        monex.parse_html
        if source == "monex"
        else minkabu.parse_html
        if source == "minkabu"
        else kabumap.parse_html
        if source == "kabumap"
        else rakuten.parse_html
    )
    return parser(html, ticker=ticker, source_url=source_url, fetched_at_utc=fetched_at_utc)


def _html(response: EquityWebSourceResponse) -> str:
    try:
        return response.body.decode(response.selected_encoding, errors="strict")
    except (LookupError, UnicodeDecodeError):
        raise ParseError("response_encoding") from None


def decode_attempt(responses: Sequence[EquityWebSourceResponse]) -> Observation:
    """Parse exactly one complete attempt using its own bytes, URLs and receipt times."""
    if not responses:
        raise ParseError("incomplete_attempt")
    if len({response.receipt_id for response in responses}) != len(responses):
        raise ParseError("duplicate_receipt")
    if len({response.role for response in responses}) != len(responses):
        raise ParseError("duplicate_response_role")
    if (
        len(
            {
                (response.attempt_id, response.source, response.requested_ticker)
                for response in responses
            }
        )
        != 1
    ):
        raise ParseError("attempt_identity")
    source = responses[0].source
    ticker = responses[0].requested_ticker
    roles = {response.role: response for response in responses}
    expected = {"page", "iframe"} if source == "rakuten" else {"page"}
    if source not in ("monex", "rakuten", "kabumap", "minkabu") or set(roles) != expected:
        raise ParseError("incomplete_attempt")
    page = roles["page"]
    if page.parent_receipt_id is not None:
        raise ParseError("parent_identity")
    response = page
    if source == "rakuten":
        response = roles["iframe"]
        if response.parent_receipt_id != page.receipt_id:
            raise ParseError("parent_identity")
        url = rakuten.iframe_url(_html(page), ticker=ticker, source_url=page.requested_url)
        if url != response.requested_url:
            raise ParseError("source_identity")
    return parse_source_html(
        _html(response),
        source=source,
        ticker=ticker,
        source_url=response.requested_url,
        fetched_at_utc=response.received_at_utc,
    )
