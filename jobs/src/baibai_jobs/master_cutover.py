"""Shared writer/consumer switch for an explicit master cutover."""

import os


def master_native_enabled() -> bool:
    value = os.environ.get("BAIBAI_MASTER_NATIVE", "true")
    if value not in {"true", "false"}:
        raise ValueError("BAIBAI_MASTER_NATIVE must be true or false")
    return value == "true"
