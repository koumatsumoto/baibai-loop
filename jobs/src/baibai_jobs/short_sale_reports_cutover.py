"""Switch short-sale-report acquisition and readers only after finite native migration."""

import os


def short_sale_reports_native_enabled() -> bool:
    value = os.environ.get("BAIBAI_SHORT_SALE_REPORTS_NATIVE", "false")
    if value not in {"true", "false"}:
        raise ValueError("BAIBAI_SHORT_SALE_REPORTS_NATIVE must be true or false")
    return value == "true"
