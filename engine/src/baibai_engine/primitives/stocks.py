from __future__ import annotations


class SecurityCodeError(ValueError):
    """A source security code cannot establish an ASCII ticker and issue suffix."""


def normalize_ticker(value: object) -> str:
    if isinstance(value, str):
        ticker = value.strip()
        if len(ticker) == 4 and ticker.isascii() and ticker.isalnum():
            return ticker.upper()
    raise SecurityCodeError(f"ticker must be a 4-character ASCII alphanumeric string: {value!r}")


def parse_security_code_parts(code: object, *, require_text: bool = False) -> tuple[str, bool]:
    """Return the ticker and primary-issue flag, not the product category.

    Native JSON price/master codes must be text. Keep the existing conversion for
    other projections so this contract does not invalidate already-built caches.
    """
    if require_text and not isinstance(code, str):
        raise SecurityCodeError(f"invalid security code: {code!r}")
    raw = str(code or "").strip()
    if not raw.isascii():
        raise SecurityCodeError(f"invalid security code: {code!r}")
    if len(raw) == 4:
        return normalize_ticker(raw), True
    if len(raw) == 5 and raw.isalnum():
        # A non-zero issue suffix must not be merged into the common-stock line.
        return normalize_ticker(raw[:4]), raw.endswith("0")
    raise SecurityCodeError(f"invalid security code: {code!r}")


def sector_33_peer_key(label: object) -> str | None:
    """その他 (9999) is not 同業; その他製品 and その他金融業 are distinct sectors."""
    return label if isinstance(label, str) and label.strip() not in {"", "0", "その他"} else None
