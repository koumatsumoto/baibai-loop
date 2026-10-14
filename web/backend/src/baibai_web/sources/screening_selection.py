"""Resolve the single latest Stock Analysis run shared by every screening surface."""

from __future__ import annotations

from baibai_web.sources.protocols import ScreeningSource
from baibai_web.sources.types import ScreeningRunRecord


def operative_screening_run(screening: ScreeningSource) -> ScreeningRunRecord | None:
    return screening.latest_run()
