"""Report economic provider acquisition separately from canonical publication."""

from __future__ import annotations

import json
import os
import sys

from baibai_batch.observability.discord import (
    DEFAULT_TIMEOUT_SECONDS,
    WEBHOOK_ENV_VAR,
    _urllib_transport,
    deliver,
    run_url,
    sanitize_one_line,
)


def main() -> int:
    env = os.environ
    acquired = env.get("ACQUIRE_RESULT", "unknown")
    published = env.get("PUBLISH_RESULT", "unknown")
    label = "OK" if acquired == published == "success" else "FAILED"
    message = (
        f"[{label}] economic {sanitize_one_line(env.get('GROUP', 'unknown'), 80)} "
        f"{sanitize_one_line(env.get('ASOF', 'unknown'), 20)}\n"
        f"取得={sanitize_one_line(acquired, 20)}; "
        f"正本反映・読戻し={sanitize_one_line(published, 20)}; 表示更新=別工程\n"
    )
    try:
        rows = json.loads(env.get("ECONOMIC_REPORT_JSON", ""))["sources"]
        failed = [str(row["source_id"]) for row in rows if row["status"] == "failed"]
        ok = sum(row["status"] == "ok" for row in rows)
        message += f"source 成功={ok}; 失敗={len(failed)}\n"
        if failed:
            message += "失敗source: " + sanitize_one_line(", ".join(failed), 1000) + "\n"
    except (ValueError, KeyError, TypeError):
        message += "source 結果=未確認\n"
    message += f"run: {run_url(env)}"
    print(message)
    failure = deliver(
        env.get(WEBHOOK_ENV_VAR, ""), message,
        timeout=DEFAULT_TIMEOUT_SECONDS, transport=_urllib_transport,
    )
    if failure is not None:
        print(f"economic notification failed: {failure}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
