"""Small shared resolver for the canonical Review Set payload."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


class ReviewSetResolutionError(ValueError):
    """Raised when a Review Set cannot be read as a unique ordered set."""


def resolve_review_set_entries(
    payload: Mapping[str, object],
) -> tuple[tuple[str, ...], dict[str, Mapping[str, object]]]:
    raw = payload.get("entries")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise ReviewSetResolutionError("entries must be an array")
    rows: dict[str, Mapping[str, object]] = {}
    ordered: list[tuple[int, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ReviewSetResolutionError("entry must be an object")
        ticker = item.get("ticker")
        position = item.get("review_position")
        if not isinstance(ticker, str) or not ticker:
            raise ReviewSetResolutionError("entry ticker is missing")
        if isinstance(position, bool) or not isinstance(position, int) or position < 1:
            raise ReviewSetResolutionError(f"review position is invalid: {ticker}")
        if ticker in rows:
            raise ReviewSetResolutionError(f"duplicate Review Set ticker: {ticker}")
        rows[ticker] = item
        ordered.append((position, ticker))
    ordered.sort()
    if [position for position, _ in ordered] != list(range(1, len(ordered) + 1)):
        raise ReviewSetResolutionError("review positions must be contiguous from 1")
    return tuple(ticker for _, ticker in ordered), rows


__all__ = ["ReviewSetResolutionError", "resolve_review_set_entries"]
