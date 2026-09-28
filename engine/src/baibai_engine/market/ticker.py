from __future__ import annotations


def normalize_ticker(value: str) -> str:
    ticker = value.strip().upper()
    if len(ticker) != 4 or not ticker.isalnum():
        raise ValueError(f"ticker must be a 4-character alphanumeric string: {value!r}")
    return ticker


class SecurityCodeError(ValueError):
    """A source security code has neither the 4- nor 5-character form."""


def parse_security_code_parts(code: object) -> tuple[str, bool]:
    """Return the ticker and whether a local issue code denotes common stock."""
    raw = str(code or "").strip().upper()
    if len(raw) == 4:
        return normalize_ticker(raw), True
    if len(raw) == 5 and raw[:4].isalnum():
        # A non-zero issue suffix must not be merged into the common-stock line.
        return normalize_ticker(raw[:4]), raw.endswith("0")
    raise SecurityCodeError(f"invalid security code: {code!r}")
