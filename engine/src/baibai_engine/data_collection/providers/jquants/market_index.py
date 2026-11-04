"""Summarize native market request evidence without reading an L2 projection."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsSource, RetainedRequest


@dataclass(frozen=True, slots=True)
class ReceiptEvidence:
    request_id: str
    source: str
    start: date
    end: date
    received_at: datetime
    records: int


@dataclass(frozen=True, slots=True)
class IncompleteReceipt:
    request_id: str
    query: tuple[tuple[str, str], ...] | None
    received_at: datetime | None


@dataclass(frozen=True, slots=True)
class NativeMarketIndex:
    source: JQuantsSource
    receipts: tuple[ReceiptEvidence, ...]
    incomplete: tuple[IncompleteReceipt, ...]


def index_market_originals(
    requests: Iterable[RetainedRequest], *, source: JQuantsSource
) -> NativeMarketIndex:
    """Distinguish full explicit day/range responses, valid empty, and incomplete.

    A filtered ticker query does not establish a market-wide receipt. This index
    is derived on demand from L1 and is not stored as a new authoritative layer.
    """
    receipts: list[ReceiptEvidence] = []
    incomplete: list[IncompleteReceipt] = []
    fingerprints: dict[str, dict[int, str]] = {}
    for item in requests:
        saved = fingerprints.setdefault(item.request_id, {})
        for page, fingerprint in item.page_fingerprints:
            existing = saved.setdefault(page, fingerprint)
            if existing != fingerprint:
                raise ValueError("conflicting retained page identity")
        request = item.request
        if request is None:
            incomplete.append(
                IncompleteReceipt(
                    item.request_id,
                    None if item.first_query is None else tuple(sorted(item.first_query.items())),
                    item.last_received_at,
                )
            )
            continue
        if request.source != source:
            raise ValueError("original source does not match its index")
        params = request.params
        try:
            if request.source == "market_calendar" and set(params) == {"from", "to"}:
                first, last = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
            else:
                key = "disc_date" if request.source == "short_sale_reports" else "date"
                if set(params) != {key}:
                    continue
                first = last = date.fromisoformat(params[key])
        except ValueError:
            incomplete.append(
                IncompleteReceipt(
                    item.request_id, tuple(sorted(params.items())), request.received_at
                )
            )
            continue
        if first > last:
            incomplete.append(
                IncompleteReceipt(
                    item.request_id, tuple(sorted(params.items())), request.received_at
                )
            )
            continue
        receipts.append(
            ReceiptEvidence(
                item.request_id,
                request.source,
                first,
                last,
                request.received_at,
                len(request.records()),
            )
        )
    return NativeMarketIndex(
        source, tuple(receipts), tuple(sorted(incomplete, key=lambda item: item.request_id))
    )
