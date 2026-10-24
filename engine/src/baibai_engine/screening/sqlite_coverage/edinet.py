"""Optional EDINET eligibility uses exactly the Screening reader's selection."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from baibai_engine.market.edinet_metrics.eligibility import read_eligible_edinet_metrics
from baibai_engine.market.edinet_metrics.store import EDINETMetricBaselineError
from baibai_engine.market.edinet_models import EDINETProviderError

from .shared import CacheCoverageIssue


def _append_edinet_metrics_coverage_issues(
    sqlite_path: Path,
    issues: list[CacheCoverageIssue],
    *,
    asof_date: date,
) -> None:
    try:
        read_eligible_edinet_metrics(sqlite_path, asof_date)
    except (EDINETMetricBaselineError, EDINETProviderError, ValueError) as exc:
        issues.append(
            CacheCoverageIssue(
                source="edinet_metrics",
                requirement=asof_date.isoformat(),
                reason=f"EDINET metrics eligibility query failed: {exc}",
            )
        )
