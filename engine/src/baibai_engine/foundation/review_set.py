"""Resolve the adopted Value / Carry Review Set at the research boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


class ReviewSetResolutionError(ValueError):
    pass


def resolve_review_set_rows(
    payload: Mapping[str, object],
) -> tuple[tuple[str, ...], dict[str, Mapping[str, object]]]:
    review_tickers_raw = payload.get("review_tickers")
    if not isinstance(review_tickers_raw, Sequence) or isinstance(review_tickers_raw, str | bytes):
        raise ReviewSetResolutionError("source selection has no Review Set")
    review_tickers = tuple(str(value) for value in review_tickers_raw)
    if len(review_tickers) != len(set(review_tickers)):
        raise ReviewSetResolutionError("Review Set contains duplicate tickers")
    core = _unique_rows(payload.get("longlist"), source_name="longlist")
    if review_tickers != tuple(core):
        raise ReviewSetResolutionError("Value / Carry Review Set must equal longlist in order")
    return review_tickers, core


def _unique_rows(value: object, *, source_name: str) -> dict[str, Mapping[str, object]]:
    if value is None:
        return {}
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ReviewSetResolutionError(f"{source_name} must be a sequence")
    rows: dict[str, Mapping[str, object]] = {}
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("ticker"), str):
            raise ReviewSetResolutionError(f"{source_name} contains an invalid row")
        ticker = str(item["ticker"])
        if ticker in rows:
            raise ReviewSetResolutionError(f"{source_name} contains duplicate ticker {ticker}")
        rows[ticker] = item
    return rows


__all__ = ["ReviewSetResolutionError", "resolve_review_set_rows"]
