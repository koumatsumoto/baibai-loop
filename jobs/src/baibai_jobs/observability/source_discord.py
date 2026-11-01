"""Source acquisition/receipt notice; canonical and serving integration are separate."""

from __future__ import annotations

import os
import sys

from .discord import (
    DEFAULT_TIMEOUT_SECONDS,
    deliver,
    render_run_notice,
    run_url,
    sanitize_one_line,
    step_label,
)


def main() -> int:
    source = sanitize_one_line(os.environ.get("SOURCE_GROUP", "unknown"))
    asof = sanitize_one_line(os.environ.get("ASOF", "unknown"))
    acquired = os.environ.get("ACQUIRE_RESULT", "unknown")
    staged = os.environ.get("STAGE_RESULT", "unknown")
    expected_unavailable = (
        source in {"economic-spglobal_pmi", "economic-tradingeconomics_pmi"}
        and os.environ.get("EXPECTED_UNAVAILABLE") == "true"
        and acquired == "failure"
        and staged == "success"
    )
    if expected_unavailable:
        label = "[UNAVAILABLE]"
    elif "failure" in {acquired, staged}:
        label = "[FAILED]"
    elif "cancelled" in {acquired, staged}:
        label = "[CANCELLED]"
    elif acquired == staged == "success":
        label = "[OK]"
    else:
        label = "[SKIPPED]" if acquired == "skipped" else "[UNKNOWN]"
    message = render_run_notice(
        label=label,
        batch=f"情報取得（{source}）",
        asof=asof if asof != "unknown" else "",
        details=[
            f"取得ジョブ: {step_label(acquired)} / private受領: {step_label(staged)}",
            *(
                ["既知のPMI HTTP 403。取得不達を保持し、繰返し通知のみ省略"]
                if expected_unavailable
                else []
            ),
            "正本・表示への反映は別スケジュールの共通統合で実施",
        ],
        url=run_url(os.environ),
    )
    print(message, flush=True)
    if not expected_unavailable and (
        acquired in {"failure", "cancelled"} or staged in {"failure", "cancelled"}
    ):
        failure = deliver(
            os.environ.get("DISCORD_WEBHOOK_URL", ""), message, timeout=DEFAULT_TIMEOUT_SECONDS
        )
        if failure is not None:
            print(f"source notification failed: {failure}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
