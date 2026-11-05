"""Shared calendar writer/consumer switch after explicit finite cutover."""

import os


def calendar_native_enabled() -> bool:
    value = os.environ.get("BAIBAI_CALENDAR_NATIVE", "false")
    if value not in {"true", "false"}:
        raise ValueError("BAIBAI_CALENDAR_NATIVE must be true or false")
    return value == "true"
