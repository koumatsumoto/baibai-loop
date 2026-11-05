"""一覧writerとconsumerを同時に切り替える有限移行中の設定。"""

import os


def edinet_list_native_enabled() -> bool:
    value = os.environ.get("BAIBAI_EDINET_LIST_NATIVE", "false")
    if value not in {"true", "false"}:
        raise ValueError("BAIBAI_EDINET_LIST_NATIVE must be true or false")
    return value == "true"
