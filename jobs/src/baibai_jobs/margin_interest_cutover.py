"""Switch margin-interest acquisition and consumers after finite native migration."""

import os


def margin_interest_native_enabled() -> bool:
    value = os.environ.get("BAIBAI_MARGIN_INTEREST_NATIVE", "false")
    if value not in {"true", "false"}:
        raise ValueError("BAIBAI_MARGIN_INTEREST_NATIVE must be true or false")
    return value == "true"
