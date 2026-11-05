"""Switch margin-alert acquisition and readers only after finite native migration."""

import os


def margin_alerts_native_enabled() -> bool:
    value = os.environ.get("BAIBAI_MARGIN_ALERTS_NATIVE", "false")
    if value not in {"true", "false"}:
        raise ValueError("BAIBAI_MARGIN_ALERTS_NATIVE must be true or false")
    return value == "true"
