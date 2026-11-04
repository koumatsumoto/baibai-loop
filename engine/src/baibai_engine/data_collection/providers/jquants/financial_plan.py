"""Plan native financial acquisition from retained L1 requests, not L2 coverage."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from baibai_engine.data_collection.providers.jquants.contracts import RetainedRequest


@dataclass(frozen=True, slots=True)
class FinancialAcquisitionPlan:
    days: tuple[date, ...]
    unknown_incomplete_requests: tuple[str, ...]


def plan_financial_acquisition(
    requests: Iterable[RetainedRequest], *, start: date, end: date, revision_overlap_days: int
) -> FinancialAcquisitionPlan:
    """Request missing full-day originals and explicitly refresh the recent tail.

    A complete empty native response is a receipt; it is not a missing value.
    Unknown-scope incomplete requests are reported separately. This plan does not
    infer whether L2 projection succeeded or whether the provider has later revisions.
    """
    if start > end:
        raise ValueError("start must not follow end")
    if revision_overlap_days < 0:
        raise ValueError("revision overlap must not be negative")
    complete: dict[date, datetime] = {}
    incomplete: dict[date, datetime | None] = {}
    unknown: list[str] = []
    pages_by_request: dict[str, dict[int, str]] = {}
    for item in requests:
        pages = pages_by_request.setdefault(item.request_id, {})
        for index, fingerprint in item.page_fingerprints:
            prior_fingerprint = pages.setdefault(index, fingerprint)
            if prior_fingerprint != fingerprint:
                raise ValueError("conflicting retained page identity")
        request = item.request
        if request is not None:
            if request.source != "financial_summaries" or set(request.params) != {"date"}:
                continue
            try:
                day = date.fromisoformat(request.params["date"])
            except ValueError:
                unknown.append(item.request_id)
                continue
            if start <= day <= end:
                previous = complete.get(day)
                if previous is None or request.received_at > previous:
                    complete[day] = request.received_at
            continue
        query = item.first_query
        if query is None or set(query) != {"date"}:
            unknown.append(item.request_id)
            continue
        try:
            day = date.fromisoformat(query["date"])
        except ValueError:
            unknown.append(item.request_id)
            continue
        if start <= day <= end:
            previous = incomplete.get(day)
            if (day not in incomplete or previous is not None) and (
                item.last_received_at is None
                or previous is None
                or item.last_received_at > previous
            ):
                incomplete[day] = item.last_received_at
    overlap_start = max(start, end - timedelta(days=revision_overlap_days))
    days = []
    day = start
    while day <= end:
        ok_at = complete.get(day)
        failed_at = incomplete.get(day)
        has_unresolved = day in incomplete and (
            failed_at is None or ok_at is None or failed_at >= ok_at
        )
        if day >= overlap_start or ok_at is None or has_unresolved:
            days.append(day)
        day += timedelta(days=1)
    return FinancialAcquisitionPlan(tuple(days), tuple(sorted(unknown)))
