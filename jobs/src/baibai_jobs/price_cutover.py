"""Price acquisition and ordinary consumers switch together after explicit migration."""

import os


def price_native_enabled() -> bool:
    value = os.environ.get("BAIBAI_PRICE_NATIVE", "false")
    if value not in {"true", "false"}:
        raise ValueError("BAIBAI_PRICE_NATIVE must be true or false")
    return value == "true"
