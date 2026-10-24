"""Source acquisition/receipt notice; canonical and serving integration are separate."""

from __future__ import annotations

import os
import sys

from .discord import DEFAULT_TIMEOUT_SECONDS, deliver, executor_label, run_url, sanitize_one_line


def main() -> int:
    source = sanitize_one_line(os.environ.get("SOURCE_GROUP", "unknown"))
    asof = sanitize_one_line(os.environ.get("ASOF", "unknown"))
    acquired = os.environ.get("ACQUIRE_RESULT", "unknown")
    staged = os.environ.get("STAGE_RESULT", "unknown")
    message = (
        f"source {source} as-of {asof}: acquisition={acquired}; private_receipts={staged}; "
        "canonical/serving integration runs on its separate schedule"
        f"\nexecutor: {executor_label(os.environ)}\nrun: {run_url(os.environ)}"
    )
    print(message, flush=True)
    if acquired in {"failure", "cancelled"} or staged in {"failure", "cancelled"}:
        failure = deliver(
            os.environ.get("DISCORD_WEBHOOK_URL", ""), message, timeout=DEFAULT_TIMEOUT_SECONDS
        )
        if failure is not None:
            print(f"source notification failed: {failure}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
