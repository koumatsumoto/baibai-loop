"""Adapt one complete J-Quants TSE master response to current listing input."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    complete_request_with_records,
)
from baibai_engine.data_collection.providers.jquants.master_native import is_master_for_day
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    JapanListingAttributes,
    ObservedListing,
)
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import (
    normalize_sector_name,
    validate_master_codes,
)

_TSE_MIC = "XTKS"
_PRODUCT_TYPES = {
    "012": "preferred_investment_security",
    "013": "reit",
    "014": "etf",
    "021": "foreign_stock",
    "022": "reit",
    "023": "etf",
    "024": "depositary_receipt",
}


@dataclass(frozen=True, slots=True)
class JQuantsListingCapture:
    session: date
    received_at: datetime
    observations: tuple[ObservedListing, ...]


def normalize_jp_listings(pages: Sequence[JQuantsPage], *, session: date) -> JQuantsListingCapture:
    """Interpret a date-only response for the caller's confirmed TSE session.

    The provider can return the next session on a holiday. Only the selected
    session is accepted; no prior master or newer response can replace it here.
    """
    request, records = complete_request_with_records(pages)
    if not is_master_for_day(request, session):
        raise JQuantsProviderError("current JP listing master requires a date-only master request")
    codes = validate_master_codes(records, session)
    return JQuantsListingCapture(
        session,
        request.received_at,
        tuple(
            _observation(record, code)
            for code, record in sorted(zip(codes, records, strict=True), key=lambda item: item[0])
        ),
    )


def _source_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JQuantsProviderError(f"current JP listing master requires a string {field}")
    return value


def _source_code(value: object, field: str) -> str | None:
    return None if value is None else _source_text(value, field)


def _observation(record: Mapping[str, object], code: str) -> ObservedListing:
    product = _source_text(record.get("ProdCat"), "ProdCat")
    name = _source_text(record.get("CoName"), "CoName")
    market_segment = _source_text(record.get("MktNm"), "MktNm")
    sector_33 = _source_text(record.get("S33Nm"), "S33Nm")
    issue_type: str | None
    if product == "011":
        # An exact five-character Code separates parallel classes. Only zero
        # is the legacy L2 domestic ordinary line; not every nonzero is preferred.
        issue_type = {"0": "common_stock", "5": "preferred_stock", "6": "preferred_stock"}.get(
            code[-1], "domestic_secondary_issue"
        )
    else:
        # J-Quants ProdCat does not separately identify ETN / infrastructure funds.
        issue_type = _PRODUCT_TYPES.get(product)
    return ObservedListing(
        (_TSE_MIC, code),
        name.strip(),
        issue_type,
        JapanListingAttributes(
            product_category=product,
            market_segment=market_segment.strip(),
            sector_33=normalize_sector_name(sector_33).strip(),
            market_segment_code=_source_code(record.get("Mkt"), "Mkt"),
            sector_33_code=_source_code(record.get("S33"), "S33"),
        ),
    )
