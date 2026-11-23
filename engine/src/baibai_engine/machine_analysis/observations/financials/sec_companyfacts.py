"""Map one pinned SEC filing and CompanyFacts receipt to an exact USD revenue observation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Literal

from baibai_engine.primitives.numbers import finite_decimal

_ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")


@dataclass(frozen=True, slots=True)
class SecCompanyFactsCapture:
    """Original bytes and a source-qualified immutable receipt reference."""

    receipt_id: str
    received_at: datetime
    body_sha256: str
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class SecFilingWitness:
    """Independently pinned filing reference and exact publication witness, if known."""

    issuer_cik: str
    accession: str
    form: Literal["10-Q", "10-Q/A"]
    receipt_id: str
    accepted_at: datetime
    published_at: datetime | None
    publication_ref: str | None
    received_at: datetime


@dataclass(frozen=True, slots=True)
class SecRevenueObservation:
    amount: Decimal
    period_start: date
    period_end: date
    filing: SecFilingWitness
    companyfacts_receipt_id: str
    companyfacts_received_at: datetime
    companyfacts_body_sha256: str
    fact_paths: tuple[str, ...]
    concept: Literal["us-gaap:Revenues"] = "us-gaap:Revenues"
    unit: Literal["USD"] = "USD"
    scope: Literal["filing_entity"] = "filing_entity"


type SecRevenueResult = (
    SecRevenueObservation
    | Literal[
        "not_yet_received",
        "publication_unverified",
        "unmapped_concept_or_unit",
        "no_fact_for_scope",
    ]
)


def observe_sec_revenue(
    capture: SecCompanyFactsCapture,
    filing: SecFilingWitness,
    *,
    expected_cik: str,
    period_start: date,
    period_end: date,
    cutoff: datetime,
) -> SecRevenueResult:
    """Interpret one fixed request; neither choose an amendment nor infer standalone periods.

    `published_at` means evidenced publication of the pinned accession, not just
    acceptance time or the time a later witness happened to find it. The two
    receipts remain independent. Absent scope is not an economic zero; invalid
    or internally contradictory evidence is an error, not an absent scope.
    """
    issuer = _cik(expected_cik)
    if type(period_start) is not date or type(period_end) is not date or period_start > period_end:
        raise ValueError("invalid requested SEC revenue period")
    asof = _clock(cutoff)
    fact_received = _clock(capture.received_at)
    filing_received = _clock(filing.received_at)
    if max(fact_received, filing_received) > asof:
        # Future bytes or attestations cannot make a past request invalid.
        return "not_yet_received"

    if _cik(filing.issuer_cik) != issuer:
        raise ValueError("filing witness CIK does not match the expected issuer")
    accession = _accession(filing.accession)
    if filing.form not in ("10-Q", "10-Q/A"):
        raise ValueError("SEC revenue requires a pinned quarterly filing form")
    if not _present_ref(capture.receipt_id) or not _present_ref(filing.receipt_id):
        raise ValueError("SEC revenue requires separate pinned receipt references")
    if capture.receipt_id == filing.receipt_id:
        raise ValueError("CompanyFacts and filing require distinct receipt references")

    accepted = _clock(filing.accepted_at)
    if (filing.published_at is None) != (filing.publication_ref is None):
        raise ValueError("SEC publication time and witness must be supplied together")
    if filing.publication_ref is not None and not _present_ref(filing.publication_ref):
        raise ValueError("invalid SEC filing publication witness reference")
    if filing.publication_ref == capture.receipt_id:
        raise ValueError("CompanyFacts receipt cannot witness filing publication")
    published = None if filing.published_at is None else _clock(filing.published_at)
    if accepted > filing_received or (
        published is not None and not accepted <= published <= filing_received
    ):
        raise ValueError("invalid SEC filing acceptance/publication/receipt order")

    document = _read_companyfacts(capture)
    if _cik(document.get("cik"), allow_integer=True) != issuer:
        raise ValueError("CompanyFacts CIK does not match the expected issuer")
    rows = _revenue_rows(document)
    # Periods are date-only, not exchange sessions. UTC+14 bounds the latest
    # possible local date without assuming the issuer's reporting timezone or close.
    latest_civil_date = (accepted + timedelta(hours=14)).date()

    amount: Decimal | None = None
    paths: list[str] = []
    for index, raw in enumerate(rows or ()):
        row = _object(raw, "revenue row")
        if _accession(row.get("accn")) != accession:
            continue
        if row.get("form") != filing.form:
            raise ValueError("SEC revenue fact form does not match the filing")
        if fact_received < accepted:
            raise ValueError("CompanyFacts fact was received before filing acceptance")
        if published is not None and fact_received < published:
            raise ValueError("CompanyFacts fact was received before witnessed filing publication")
        start, end = _reported_date(row.get("start")), _reported_date(row.get("end"))
        if start > end:
            raise ValueError("invalid SEC revenue period")
        if end > latest_civil_date:
            raise ValueError("SEC revenue period end is after the filing acceptance date bound")
        if (start, end) != (period_start, period_end):
            continue
        raw_amount = row.get("val")
        if type(raw_amount) not in (int, Decimal):
            raise ValueError("SEC revenue value must be a JSON number")
        value = finite_decimal(raw_amount)
        if amount is not None and amount != value:
            raise ValueError("conflicting SEC revenue amounts for one accession and period")
        # Keep every source location. Total order only breaks a representation tie
        # between equal amounts; it does not attest reported accounting precision.
        if amount is None or value.compare_total(amount) < 0:
            amount = value
        paths.append(f"/facts/us-gaap/Revenues/units/USD/{index}")

    if published is None:
        return "publication_unverified"
    if rows is None:
        return "unmapped_concept_or_unit"
    if amount is None:
        return "no_fact_for_scope"
    return SecRevenueObservation(
        amount=amount,
        period_start=period_start,
        period_end=period_end,
        filing=replace(filing, issuer_cik=issuer),
        companyfacts_receipt_id=capture.receipt_id,
        companyfacts_received_at=fact_received,
        companyfacts_body_sha256=capture.body_sha256,
        fact_paths=tuple(paths),
    )


def _present_ref(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _clock(value: object) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("SEC evidence clocks require a timezone offset")
    return value.astimezone(UTC)


def _cik(value: object, *, allow_integer: bool = False) -> str:
    if allow_integer and type(value) is int and 1 <= value <= 9_999_999_999:
        return f"{value:010d}"
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value) and int(value) > 0:
        return value.zfill(10)
    raise ValueError("invalid SEC CIK")


def _accession(value: object) -> str:
    if not isinstance(value, str) or not _ACCESSION.fullmatch(value):
        raise ValueError("invalid SEC accession")
    return value


def _reported_date(value: object) -> date:
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("invalid SEC revenue period") from exc
        if parsed.isoformat() == value:
            return parsed
    raise ValueError("invalid SEC revenue period")


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"invalid SEC CompanyFacts {label}")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate object key")
    return result


def _reject_constant(raw: str) -> object:
    raise ValueError(f"non-JSON number: {raw}")


def _read_companyfacts(capture: SecCompanyFactsCapture) -> dict[str, object]:
    body: object = capture.body
    if not isinstance(body, bytes) or sha256(body).hexdigest() != capture.body_sha256:
        raise ValueError("CompanyFacts body does not match its receipt SHA-256")
    try:
        document = json.loads(
            body.decode("utf-8"),
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (ValueError, ArithmeticError) as exc:
        raise ValueError("invalid SEC CompanyFacts JSON") from exc
    return _object(document, "document")


def _revenue_rows(document: dict[str, object]) -> list[object] | None:
    facts = _object(document.get("facts"), "facts")
    if "us-gaap" not in facts:
        return None
    gaap = _object(facts["us-gaap"], "us-gaap taxonomy")
    if "Revenues" not in gaap:
        return None
    revenue = _object(gaap["Revenues"], "Revenues concept")
    units = _object(revenue.get("units"), "Revenues units")
    if "USD" not in units:
        return None
    rows = units["USD"]
    if not isinstance(rows, list):
        raise ValueError("invalid SEC CompanyFacts USD facts")
    return rows
