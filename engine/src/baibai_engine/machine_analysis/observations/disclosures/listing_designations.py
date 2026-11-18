"""監理・整理の指定を、判断時計までに完了した特定のJPX原本から読む。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from baibai_engine.data_collection.providers.jpx.receipts import (
    JPXBundleError,
    read_complete_bundle,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_decode import (
    parse_current_listing_designations,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import JPXProviderError
from baibai_engine.machine_analysis.observations.disclosures.jpx_sources import JPX_REGULATION_URLS
from baibai_engine.primitives.time import JST


class ListingDesignationCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: Literal["jpx_designation_page_at_receipt_not_live_status"] = (
        "jpx_designation_page_at_receipt_not_live_status"
    )
    status: Literal["observed", "no_eligible_bundle", "unreadable_bundle", "ambiguous_bundle"]
    checked_at: datetime
    source_url: str = JPX_REGULATION_URLS["整理銘柄"]
    bundle_id: str | None = None
    snapshot_as_of: date | None = None
    completed_at: datetime | None = None
    page_received_at: datetime | None = None
    # Null: evidence unavailable. An empty tuple: not listed on this specific page.
    designations_by_ticker: dict[str, tuple[str, ...]] | None = None


def read_listing_designations(
    connection: sqlite3.Connection | None, *, tickers: tuple[str, ...], known_at: datetime
) -> ListingDesignationCheck:
    """同じmarket read transaction内。新規取得・既発行の規制表の再生成はしない。"""
    if known_at.utcoffset() is None:
        raise ValueError("JPX designation cutoff requires a timezone")
    unavailable = ListingDesignationCheck(status="no_eligible_bundle", checked_at=known_at)
    if connection is None or not tickers:
        return unavailable
    rows = connection.execute(
        "SELECT bundle_id, asof_date, completed_at_utc FROM jpx_source_bundles "
        "WHERE collection_name='regulations' AND asof_date<=? AND completed_at_utc<=? "
        "ORDER BY asof_date DESC, completed_at_utc DESC LIMIT 2",
        (
            known_at.astimezone(JST).date().isoformat(),
            known_at.astimezone(UTC).isoformat(timespec="microseconds"),
        ),
    ).fetchall()
    if not rows:
        return unavailable
    identity, source_date, completed = rows[0]
    if len(rows) > 1 and tuple(rows[1])[1:] == (source_date, completed):
        return ListingDesignationCheck(status="ambiguous_bundle", checked_at=known_at)
    bundle_id = str(identity)
    # Earlier publications only extracted 整理銘柄. A missing old projection row
    # cannot establish that the ticker wasn't under supervision at this time.
    try:
        bundle = read_complete_bundle(connection, bundle_id)
        resources = {item.resource_name: item.response for item in bundle.responses}
        if {k: v.requested_url for k, v in resources.items()} != dict(JPX_REGULATION_URLS):
            raise JPXBundleError("unexpected JPX regulation bundle sources")
        receipt = resources["整理銘柄"]
        designations = parse_current_listing_designations(
            receipt.body, receipt.requested_url, receipt.content_type
        )
    except (JPXBundleError, JPXProviderError):
        return ListingDesignationCheck(
            status="unreadable_bundle", checked_at=known_at, bundle_id=bundle_id
        )
    return ListingDesignationCheck(
        status="observed",
        checked_at=known_at,
        bundle_id=bundle_id,
        snapshot_as_of=bundle.asof_date,
        completed_at=bundle.completed_at,
        page_received_at=receipt.received_at,
        designations_by_ticker={ticker: designations.get(ticker, ()) for ticker in tickers},
    )
