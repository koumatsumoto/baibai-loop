from __future__ import annotations


class SecurityCodeError(ValueError):
    """A source security code cannot establish an ASCII ticker and issue suffix."""


def normalize_ticker(value: str) -> str:
    ticker = value.strip()
    if len(ticker) != 4 or not ticker.isascii() or not ticker.isalnum():
        raise SecurityCodeError(
            f"ticker must be a 4-character ASCII alphanumeric string: {value!r}"
        )
    return ticker.upper()


def parse_security_code_parts(code: object) -> tuple[str, bool]:
    """Return the ticker and whether the issue suffix is zero, not its product category."""
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
