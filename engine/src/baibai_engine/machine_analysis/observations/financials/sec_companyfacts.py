"""Read issuer-wide USD revenue concepts from one pinned filing and CompanyFacts receipt."""

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

type SecRevenueConcept = Literal[
    "us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
]
# Enumeration is for evidence, not preference or a net-sales definition.
_CONCEPTS: tuple[SecRevenueConcept, ...] = (
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
)


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
class SecPublicFilingObservation:
    """An independently pinned original SEC filing-index receipt, not a release time."""

    issuer_cik: str
    accession: str
    observed_at: datetime
    receipt_id: str
    index_url: str


@dataclass(frozen=True, slots=True)
class SecRevenueObservation:
    """An entire-filing-entity observation, not a cross-concept net-sales mapping."""

    amount: Decimal
    period_start: date
    period_end: date
    filing: SecFilingWitness
    companyfacts_receipt_id: str
    companyfacts_received_at: datetime
    companyfacts_body_sha256: str
    fact_paths: tuple[str, ...]
    concept: SecRevenueConcept
    unit: Literal["USD"] = "USD"
    scope: Literal["filing_entity"] = "filing_entity"
    publicly_observed_by: SecPublicFilingObservation | None = None


@dataclass(frozen=True, slots=True)
class SecRevenueAmbiguity:
    """Both native concepts match. Preserve evidence without choosing a sales measure."""

    candidates: tuple[SecRevenueObservation, ...]

    @property
    def reason(self) -> Literal["conflicting_amounts", "semantic_equivalence_unverified"]:
        if len({candidate.amount for candidate in self.candidates}) > 1:
            return "conflicting_amounts"
        return "semantic_equivalence_unverified"


type SecRevenueResult = (
    SecRevenueObservation
    | SecRevenueAmbiguity
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
    publicly_observed_by: SecPublicFilingObservation | None = None,
) -> SecRevenueResult:
    """Interpret one fixed request; neither choose an amendment nor infer standalone periods.

    `published_at` means evidenced publication of the pinned accession, not just
    acceptance time or the time a later witness happened to find it.
    `publicly_observed_by` can attest availability only from its own original
    SEC index receipt onward. It does not become a publication timestamp.
    Absent scope is not an economic zero; internally contradictory evidence
    within one concept is an error.
    Different issuer-wide concepts are not interchangeable even at equal amounts.
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
    if not _valid_receipt_ref(capture.receipt_id) or not _valid_receipt_ref(filing.receipt_id):
        raise ValueError("SEC revenue requires separate pinned receipt references")
    if capture.receipt_id == filing.receipt_id:
        raise ValueError("CompanyFacts and filing require distinct receipt references")

    accepted = _clock(filing.accepted_at)
    if (filing.published_at is None) != (filing.publication_ref is None):
        raise ValueError("SEC publication time and witness must be supplied together")
    if filing.publication_ref is not None and not _valid_receipt_ref(filing.publication_ref):
        raise ValueError("invalid SEC filing publication witness reference")
    if filing.publication_ref == capture.receipt_id:
        raise ValueError("CompanyFacts receipt cannot witness filing publication")
    published = None if filing.published_at is None else _clock(filing.published_at)
    if accepted > filing_received or (
        published is not None and not accepted <= published <= filing_received
    ):
        raise ValueError("invalid SEC filing acceptance/publication/receipt order")

    verified_filing = replace(filing, issuer_cik=issuer)
    observed = _public_index_asof(
        publicly_observed_by,
        verified_filing,
        asof=asof,
        accepted=accepted,
        published=published,
        companyfacts_receipt_id=capture.receipt_id,
    )
    known_public = published is not None or observed is not None

    document = _read_companyfacts(capture)
    if _cik(document.get("cik"), allow_integer=True) != issuer:
        raise ValueError("CompanyFacts CIK does not match the expected issuer")
    rows_by_concept = _revenue_rows(document)
    # Periods are date-only, not exchange sessions. UTC+14 bounds the latest
    # possible local date without assuming the issuer's reporting timezone or close.
    latest_civil_date = (accepted + timedelta(hours=14)).date()
    matches: list[SecRevenueObservation] = []
    for concept, rows in rows_by_concept.items():
        tag = concept.removeprefix("us-gaap:")
        amount: Decimal | None = None
        paths: list[str] = []
        for index, raw in enumerate(rows):
            row = _object(raw, "revenue row")
            if _accession(row.get("accn")) != accession:
                continue
            if row.get("form") != filing.form:
                raise ValueError("SEC revenue fact form does not match the filing")
            if fact_received < accepted:
                raise ValueError("CompanyFacts fact was received before filing acceptance")
            if published is not None and fact_received < published:
                raise ValueError(
                    "CompanyFacts fact was received before witnessed filing publication"
                )
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
            paths.append(f"/facts/us-gaap/{tag}/units/USD/{index}")
        if amount is not None and known_public:
            matches.append(
                SecRevenueObservation(
                    amount=amount,
                    period_start=period_start,
                    period_end=period_end,
                    filing=verified_filing,
                    companyfacts_receipt_id=capture.receipt_id,
                    companyfacts_received_at=fact_received,
                    companyfacts_body_sha256=capture.body_sha256,
                    fact_paths=tuple(paths),
                    concept=concept,
                    publicly_observed_by=observed,
                )
            )

    if not known_public:
        return "publication_unverified"
    if not rows_by_concept:
        return "unmapped_concept_or_unit"
    if not matches:
        return "no_fact_for_scope"
    if len(matches) == 1:
        return matches[0]
    return SecRevenueAmbiguity(tuple(matches))


def _public_index_asof(
    observation: SecPublicFilingObservation | None,
    filing: SecFilingWitness,
    *,
    asof: datetime,
    accepted: datetime,
    published: datetime | None,
    companyfacts_receipt_id: str,
) -> SecPublicFilingObservation | None:
    if observation is None:
        return None
    observed_at = _clock(observation.observed_at)
    if observed_at > asof:
        return None
    if _cik(observation.issuer_cik) != filing.issuer_cik:
        raise ValueError("SEC public observation CIK does not match the filing")
    if _accession(observation.accession) != filing.accession:
        raise ValueError("SEC public observation accession does not match the filing")
    if not _valid_receipt_ref(observation.receipt_id):
        raise ValueError("invalid SEC public observation receipt reference")
    if observation.receipt_id in (
        companyfacts_receipt_id,
        filing.receipt_id,
        filing.publication_ref,
    ):
        raise ValueError("SEC public observation requires a distinct original index receipt")
    accession_folder = filing.accession.replace("-", "")
    cik_folder = filing.issuer_cik.lstrip("0")
    expected_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_folder}/{accession_folder}/"
        f"{filing.accession}-index.htm"
    )
    if observation.index_url != expected_url:
        raise ValueError("SEC public observation index URL does not match the original filing")
    if observed_at < accepted:
        raise ValueError("SEC public observation is before filing acceptance")
    if published is not None and observed_at < published:
        raise ValueError("SEC public observation is before witnessed filing publication")
    return replace(observation, issuer_cik=filing.issuer_cik, observed_at=observed_at)


def _valid_receipt_ref(value: object) -> bool:
    """Receipt references are opaque IDs, not display labels."""
    return isinstance(value, str) and bool(value) and not any(ch.isspace() for ch in value)


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


def _revenue_rows(document: dict[str, object]) -> dict[SecRevenueConcept, list[object]]:
    facts = _object(document.get("facts"), "facts")
    if "us-gaap" not in facts:
        return {}
    gaap = _object(facts["us-gaap"], "us-gaap taxonomy")
    result: dict[SecRevenueConcept, list[object]] = {}
    for concept in _CONCEPTS:
        tag = concept.removeprefix("us-gaap:")
        if tag not in gaap:
            continue
        revenue = _object(gaap[tag], f"{tag} concept")
        units = _object(revenue.get("units"), f"{tag} units")
        if "USD" not in units:
            continue
        rows = units["USD"]
        if not isinstance(rows, list):
            raise ValueError("invalid SEC CompanyFacts USD facts")
        result[concept] = rows
    return result
