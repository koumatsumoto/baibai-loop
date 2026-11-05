"""財務writerとdaily consumerを一組で切り替える移行中だけの設定。"""

import os


def financial_native_enabled() -> bool:
    value = os.environ.get("BAIBAI_FINANCIAL_NATIVE", "false")
    if value not in {"true", "false"}:
        raise ValueError("BAIBAI_FINANCIAL_NATIVE must be true or false")
    return value == "true"
