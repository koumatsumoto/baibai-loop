"""有限移行済みの一覧writerとconsumerを揃える通常設定。"""

import os


def edinet_list_native_enabled() -> bool:
    value = os.environ.get("BAIBAI_EDINET_LIST_NATIVE", "true")
    if value not in {"true", "false"}:
        raise ValueError("BAIBAI_EDINET_LIST_NATIVE must be true or false")
    return value == "true"
