"""Plan native financial acquisition without relying on previous L2 coverage."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

from baibai_engine.data_collection.providers.jquants.contracts import RetainedRequest
from baibai_engine.data_collection.providers.jquants.native_index import (
    NativeAcquisitionPlan,
    index_market_originals,
    plan_required_days,
)


def plan_financial_acquisition(
    requests: Iterable[RetainedRequest], *, start: date, end: date, revision_overlap_days: int
) -> NativeAcquisitionPlan:
    """Request missing source days and the explicit recent revision window."""
    if start > end:
        raise ValueError("start must not follow end")
    if revision_overlap_days < 0:
        raise ValueError("revision overlap must not be negative")
    index = index_market_originals(requests, source="financial_summaries")
    days = (start + timedelta(days=offset) for offset in range((end - start).days + 1))
    missing = plan_required_days(index, days=days, require_nonempty=False)
    recent_start = max(start, end - timedelta(days=revision_overlap_days))
    recent = (
        recent_start + timedelta(days=offset) for offset in range((end - recent_start).days + 1)
    )
    return NativeAcquisitionPlan(
        tuple(sorted(set(missing.days) | set(recent))), missing.unknown_incomplete_requests
    )
