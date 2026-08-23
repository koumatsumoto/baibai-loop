"""Exact source resolution for the current ``longlist`` / ``longlist_alt`` wire."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


class ReviewSetResolutionError(ValueError):
    pass


_COMMON_SOURCE_FIELDS = (
    "ticker",
    "name",
    "expected_return_pct",
    "fair_value_anchor_yen",
    "market_price_yen",
    "liquidity_status",
    "estimate_snapshot",
)


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
    alt_block = payload.get("longlist_alt")
    alt_entries = alt_block.get("entries") if isinstance(alt_block, Mapping) else None
    alternative = _unique_rows(alt_entries, source_name="longlist_alt.entries")

    rows: dict[str, Mapping[str, object]] = {}
    for ticker in review_tickers:
        core_row = core.get(ticker)
        alt_row = alternative.get(ticker)
        if core_row is not None:
            if alt_row is not None:
                if alt_row.get("overlaps_value_carry_longlist") is not True:
                    raise ReviewSetResolutionError(
                        f"{ticker} Earnings overlap is missing its overlap flag"
                    )
                conflicts = [
                    field
                    for field in _COMMON_SOURCE_FIELDS
                    if core_row.get(field) != alt_row.get(field)
                ]
                if conflicts:
                    raise ReviewSetResolutionError(
                        f"{ticker} Review Set source facts conflict: {', '.join(conflicts)}"
                    )
            rows[ticker] = core_row
            continue
        if alt_row is None:
            raise ReviewSetResolutionError(f"{ticker} has no Review Set source row")
        if alt_row.get("overlaps_value_carry_longlist") is True:
            raise ReviewSetResolutionError(
                f"{ticker} declares Value / Carry overlap but has no Core source row"
            )
        rows[ticker] = alt_row
    return review_tickers, rows


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
