"""Bind a successful daily model publication to its own GitHub Actions attempt."""

from __future__ import annotations

import os
import re


def daily_run(value: str) -> str:
    if re.fullmatch(r"[1-9][0-9]{0,19}-[1-9][0-9]{0,9}", value) is None:
        raise ValueError("invalid original daily run ID and attempt")
    return value


def current_daily_run() -> str:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    ref = os.environ.get("GITHUB_REF", "")
    if (
        not repository
        or not ref
        or os.environ.get("GITHUB_WORKFLOW_REF")
        != f"{repository}/.github/workflows/cloud-daily-batch-current.yml@{ref}"
    ):
        raise ValueError("daily publication requires the current daily workflow")
    return daily_run(
        f"{os.environ.get('GITHUB_RUN_ID', '')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '')}"
    )
