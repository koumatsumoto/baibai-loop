from __future__ import annotations


def normalize_ticker(value: str) -> str:
    ticker = value.strip().upper()
    if len(ticker) != 4 or not ticker.isalnum():
        raise ValueError(f"ticker must be a 4-character alphanumeric string: {value!r}")
    return ticker
