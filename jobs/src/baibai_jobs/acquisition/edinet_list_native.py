"""Acquire and publish complete EDINET list receipts without a market/L2 input."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.list_projection import (
    document_list_rechecked_asof,
    project_document_list,
)
from baibai_engine.data_collection.providers.edinet.models import (
    EdinetAcquisitionError,
    EdinetResponse,
)
from baibai_engine.data_collection.providers.edinet.receipts import EdinetListReceipt
from baibai_engine.data_collection.transport.http import AcquisitionDeadlineExceeded

_JST = ZoneInfo("Asia/Tokyo")


class ListClient(Protocol):
    def fetch_list(self, on_date: date) -> EdinetResponse: ...


def acquire_lists(
    owner: EdinetListL1,
    client: ListClient,
    *,
    start: date,
    end: date,
    today: date | None = None,
    force_history: bool = False,
) -> dict[str, object]:
    """Retain raw first, preserve the existing today→history proof and request scope."""
    if start > end:
        raise ValueError("EDINET list start must be on or before end")
    today = today or datetime.now(_JST).date()
    if end > today:
        raise ValueError("EDINET list acquisition cannot request a future day")
    owner.require_clean_work()
    fixed = owner.freeze()
    received: dict[date, list[EdinetListReceipt]] = defaultdict(list)
    for item in fixed.iter_receipts():
        received[item.doc_date].append(item.decoded())
    additions: list[NativeListReceipt] = []
    fetched: set[date] = set()
    failed: list[dict[str, str]] = []
    rows = 0

    def obtain(day: date, *, force: bool) -> int:
        if not force and received[day]:
            projection = project_document_list(received[day])
            if projection.error is None:
                return len(projection.documents)
        response = client.fetch_list(day)
        retained = NativeListReceipt.from_response(day, response)
        # Each day is an independently completed source request. Confirm its
        # original before decoding or making another HTTP request.
        owner.publish([retained])
        additions.append(retained)
        received[day].append(retained.decoded())
        fetched.add(day)
        projection = project_document_list(received[day])
        if projection.error is not None:
            # The provider body remains a receipt, including malformed JSON.
            raise ValueError("EDINET list response is not eligible")
        return len(projection.documents)

    active = today
    try:
        # The real current-day response precedes historical rechecks, including
        # an explicit past asof. A saved L2 clock cannot substitute for this GET.
        today_rows = obtain(today, force=True)
        active = end
        rows += today_rows if end == today else obtain(end, force=True)
        for day in sorted(received):
            if day >= end or day in fetched or not received[day]:
                continue
            projection = project_document_list(received[day])
            selected = projection.selected_receipt
            proof = (
                received.get(selected.received_at.astimezone(_JST).date(), [])
                if selected is not None
                else []
            )
            if document_list_rechecked_asof(projection, proof) is None:
                active = day
                rows += obtain(day, force=True)
        cursor = start
        while cursor <= end:
            if cursor not in fetched:
                active = cursor
                rows += obtain(cursor, force=force_history)
            cursor += timedelta(days=1)
    except (EdinetAcquisitionError, AcquisitionDeadlineExceeded, ValueError, OSError) as error:
        failed.append({"day": active.isoformat(), "reason": type(error).__name__})
    published = owner.freeze()
    return {
        "source": "edinet-list",
        "start": start.isoformat(),
        "asof": end.isoformat(),
        "status": "failed" if failed else "received",
        "received_count": len(additions),
        "documents": rows,
        "failed": failed,
        "l1_identity": published.identity,
    }
