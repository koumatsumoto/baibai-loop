"""Build selected L2 observations from one fixed native L1 release."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from baibai_engine.data_collection.providers.jquants.lake_receipts import (
    iter_fixed_retained_requests,
)
from baibai_engine.data_collection.providers.jquants.snapshot_contracts import JQUANTS_SOURCE_PAGES
from baibai_engine.machine_analysis.observations.financials.from_l1 import (
    FinancialDay,
    materialize_financial_days,
    rebuild_financial_days,
)
from baibai_engine.machine_analysis.observations.prices.from_l1 import (
    PriceDay,
    materialize_price_days,
    rebuild_price_days,
)
from baibai_engine.storage.snapshots.duck import LakeSession
from baibai_engine.storage.snapshots.objects import LakeObjectCache
from baibai_engine.storage.snapshots.reader import FixedRelease, accepted_dataset


@dataclass(frozen=True, slots=True)
class RebuildReport:
    release_id: str
    manifest_sha256: str
    l1_coverage_status: str
    requested_days: int
    received_days: int
    unavailable_days: int
    partial_days: int
    rows: int


def rebuild_fixed_days(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    source: Literal["financial_summaries", "daily_bars"],
    days: Iterable[date],
    known_at: datetime,
) -> RebuildReport:
    """Rebuild one observation family without consuming any previous L2."""
    if known_at.utcoffset() is None:
        raise ValueError("known_at requires a timezone")
    wanted = tuple(sorted(set(days)))
    dataset = accepted_dataset(release, JQUANTS_SOURCE_PAGES)
    requests = (
        iter_fixed_retained_requests(
            session, release=release, cache=cache, source=source, known_at=known_at
        )
        if wanted
        else ()
    )
    coverage = release.dataset_manifest(dataset.name).coverage_status
    values: list[FinancialDay | PriceDay]
    if source == "financial_summaries":
        financial = rebuild_financial_days(requests, days=wanted)
        materialize_financial_days(
            session.connection, financial.values(), l1_coverage_status=coverage
        )
        values = list(financial.values())
    elif source == "daily_bars":
        prices = rebuild_price_days(requests, days=wanted)
        materialize_price_days(session.connection, prices.values(), l1_coverage_status=coverage)
        values = list(prices.values())
    else:
        raise ValueError("unsupported observation family")
    return RebuildReport(
        release_id=release.release_id,
        manifest_sha256=release.manifest_sha256,
        l1_coverage_status=coverage,
        requested_days=len(wanted),
        received_days=sum(item.request_id is not None for item in values),
        unavailable_days=sum(item.request_id is None for item in values),
        partial_days=sum(
            item.status == "partial" or item.incomplete_requests > 0 for item in values
        ),
        rows=sum(len(item.rows) for item in values),
    )
