"""一覧・ZIP/scopeのwriterとconsumerを揃える設定。既存env名を維持する。"""

import os


def edinet_list_native_enabled() -> bool:
    value = os.environ.get("BAIBAI_EDINET_LIST_NATIVE", "true")
    if value not in {"true", "false"}:
        raise ValueError("BAIBAI_EDINET_LIST_NATIVE must be true or false")
    return value == "true"
