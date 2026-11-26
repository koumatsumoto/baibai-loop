"""Read issuer-wide USD observations from one pinned filing and CompanyFacts receipt."""

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
_REVENUE_CONCEPTS: tuple[SecRevenueConcept, ...] = (
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
    form: Literal["10-Q", "10-Q/A", "10-K"]
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
    report_end: date


@dataclass(frozen=True, slots=True)
class SecIssuerUsdObservation:
    """A native entity-wide USD amount for one exact reported duration."""

    amount: Decimal
    period_start: date
    period_end: date
    filing: SecFilingWitness
    companyfacts_receipt_id: str
    companyfacts_received_at: datetime
    companyfacts_body_sha256: str
    fact_paths: tuple[str, ...]
    concept: SecReportedConcept
    unit: Literal["USD"] = "USD"
    scope: Literal["filing_entity"] = "filing_entity"
    publicly_observed_by: SecPublicFilingObservation | None = None


@dataclass(frozen=True, slots=True)
class SecRevenueAmbiguity:
    """Both native concepts match. Preserve evidence without choosing a sales measure."""

    candidates: tuple[SecIssuerUsdObservation, ...]

    @property
    def reason(self) -> Literal["conflicting_amounts", "semantic_equivalence_unverified"]:
        if len({candidate.amount for candidate in self.candidates}) > 1:
            return "conflicting_amounts"
        return "semantic_equivalence_unverified"


type _UnavailableReceipt = Literal[
    "not_yet_received", "publication_unverified", "filing_period_mismatch"
]
type _MissingAmount = Literal["unmapped_concept_or_unit", "no_fact_for_scope"]
type SecUsdResult = (
    SecIssuerUsdObservation | SecRevenueAmbiguity | _UnavailableReceipt | _MissingAmount
)

type SecReportedConcept = (
    SecRevenueConcept
    | Literal[
        "us-gaap:OperatingIncomeLoss",
        "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
    ]
)


@dataclass(frozen=True, slots=True)
class SecReportedDuration:
    """Exact dates taken from a column of the pinned financial statements."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if type(self.start) is not date or type(self.end) is not date or self.start > self.end:
            raise ValueError("invalid requested SEC reporting duration")

    @property
    def fits_regular_quarter(self) -> bool:
        return 89 <= (self.end - self.start).days + 1 <= 98

    @property
    def fits_regular_fiscal_year(self) -> bool:
        return (self.end - self.start).days + 1 in {364, 365, 366, 371}


type _Metric = Literal["revenue", "operating_income", "operating_cash_flow", "ppe_cash_paid"]
type _Basis = Literal["quarter", "year_to_date"]


@dataclass(frozen=True, slots=True)
class SecStatementComparison:
    metric: _Metric
    basis: _Basis
    current_period: SecReportedDuration
    prior_period: SecReportedDuration
    current: SecUsdResult | Literal["invalid_fact"]
    prior: SecUsdResult | Literal["invalid_fact"]


@dataclass(frozen=True, slots=True)
class _PinnedCompanyFacts:
    capture: SecCompanyFactsCapture
    filing: SecFilingWitness
    received_at: datetime
    accepted_at: datetime
    published_at: datetime | None
    publicly_observed_by: SecPublicFilingObservation | None
    gaap: dict[str, object]

    @property
    def known_public(self) -> bool:
        return self.published_at is not None or self.publicly_observed_by is not None


def observe_sec_revenue(
    capture: SecCompanyFactsCapture,
    filing: SecFilingWitness,
    *,
    expected_cik: str,
    period_start: date,
    period_end: date,
    cutoff: datetime,
    publicly_observed_by: SecPublicFilingObservation | None = None,
) -> SecUsdResult:
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
    period = SecReportedDuration(period_start, period_end)
    if filing.form == "10-K" and not period.fits_regular_fiscal_year:
        raise ValueError("SEC annual revenue requires a regular full fiscal year")
    source = _pin_companyfacts(
        capture,
        filing,
        issuer=issuer,
        cutoff=cutoff,
        publicly_observed_by=publicly_observed_by,
        current_period_end=period.end,
    )
    if isinstance(source, str):
        return source
    return _observe(source, period, _REVENUE_CONCEPTS)


def observe_sec_comparatives(
    capture: SecCompanyFactsCapture,
    filing: SecFilingWitness,
    *,
    expected_cik: str,
    reporting_quarter: str,
    current_quarter: SecReportedDuration,
    prior_quarter: SecReportedDuration,
    current_fiscal_start: date,
    prior_fiscal_start: date,
    cutoff: datetime,
    publicly_observed_by: SecPublicFilingObservation | None = None,
) -> tuple[SecStatementComparison, ...] | _UnavailableReceipt:
    """Read the operator-selected current and prior-year columns of one 10-Q.

    Specify the reporting quarter as confirmed in the filing, not the
    CompanyFacts fy/fp/frame or an inferred period. Only normal Q1-Q3
    and the corresponding period of the preceding fiscal year are in scope.
    No cash-flow derivation, extrapolation or valuation.
    """
    if filing.form not in ("10-Q", "10-Q/A"):
        raise ValueError("SEC quarterly comparisons require a pinned 10-Q or 10-Q/A")
    current_ytd = SecReportedDuration(current_fiscal_start, current_quarter.end)
    prior_ytd = SecReportedDuration(prior_fiscal_start, prior_quarter.end)
    positions = (
        _quarter_position(current_quarter, current_ytd),
        _quarter_position(prior_quarter, prior_ytd),
    )
    if positions != (reporting_quarter, reporting_quarter):
        raise ValueError("SEC periods do not match the selected reporting quarter")
    corresponding_dates = (
        (current_quarter.end, prior_quarter.end),
        (current_quarter.start, prior_quarter.start),
        (current_ytd.start, prior_ytd.start),
    )
    if not all(364 <= (current - prior).days <= 371 for current, prior in corresponding_dates):
        raise ValueError("SEC comparison requires the preceding fiscal year")

    source = _pin_companyfacts(
        capture,
        filing,
        issuer=_cik(expected_cik),
        cutoff=cutoff,
        publicly_observed_by=publicly_observed_by,
        current_period_end=current_quarter.end,
    )
    if isinstance(source, str):
        return source
    if not source.known_public:
        return "publication_unverified"

    slots: tuple[tuple[_Metric, _Basis, tuple[SecReportedConcept, ...]], ...] = (
        ("revenue", "quarter", _REVENUE_CONCEPTS),
        ("operating_income", "quarter", ("us-gaap:OperatingIncomeLoss",)),
        (
            "operating_cash_flow",
            "year_to_date",
            ("us-gaap:NetCashProvidedByUsedInOperatingActivities",),
        ),
        ("ppe_cash_paid", "year_to_date", ("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",)),
    )

    def amount(
        period: SecReportedDuration, concepts: tuple[SecReportedConcept, ...]
    ) -> SecUsdResult | Literal["invalid_fact"]:
        try:
            return _observe(source, period, concepts)
        except (ValueError, ArithmeticError):
            return "invalid_fact"

    rows = []
    for metric, basis, concepts in slots:
        current, prior = (
            (current_quarter, prior_quarter) if basis == "quarter" else (current_ytd, prior_ytd)
        )
        rows.append(
            SecStatementComparison(
                metric, basis, current, prior, amount(current, concepts), amount(prior, concepts)
            )
        )
    return tuple(rows)


def _quarter_position(quarter: SecReportedDuration, ytd: SecReportedDuration) -> str:
    if not quarter.fits_regular_quarter:
        raise ValueError("SEC comparatives require regular standalone quarters")
    # The fiscal-year start must precede the quarter by zero, one or two
    # regular quarters. These coarse bounds reject arbitrary cumulative spans,
    # not establish an issuer's actual fiscal calendar.
    elapsed = (quarter.start - ytd.start).days
    if elapsed == 0:
        return "Q1"
    if 89 <= elapsed <= 98:
        return "Q2"
    if 178 <= elapsed <= 196:
        return "Q3"
    raise ValueError("SEC year-to-date dates do not describe a regular Q1-Q3")


def _pin_companyfacts(
    capture: SecCompanyFactsCapture,
    filing: SecFilingWitness,
    *,
    issuer: str,
    cutoff: datetime,
    publicly_observed_by: SecPublicFilingObservation | None,
    current_period_end: date,
) -> _PinnedCompanyFacts | Literal["not_yet_received", "filing_period_mismatch"]:
    asof = _clock(cutoff)
    fact_received = _clock(capture.received_at)
    filing_received = _clock(filing.received_at)
    if max(fact_received, filing_received) > asof:
        # Future bytes or attestations cannot make a past request invalid.
        return "not_yet_received"

    if _cik(filing.issuer_cik) != issuer:
        raise ValueError("filing witness CIK does not match the expected issuer")
    _accession(filing.accession)
    if filing.form not in ("10-Q", "10-Q/A", "10-K"):
        raise ValueError("unsupported pinned SEC filing form")
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
    # CompanyFacts can carry prior-year columns under the current accession.
    if observed is not None and observed.report_end != current_period_end:
        return "filing_period_mismatch"
    document = _read_companyfacts(capture)
    if _cik(document.get("cik"), allow_integer=True) != issuer:
        raise ValueError("CompanyFacts CIK does not match the expected issuer")
    facts = _object(document.get("facts"), "facts")
    gaap = _object(facts.get("us-gaap", {}), "us-gaap taxonomy")
    return _PinnedCompanyFacts(
        capture, verified_filing, fact_received, accepted, published, observed, gaap
    )


def _observe(
    source: _PinnedCompanyFacts,
    period: SecReportedDuration,
    concepts: tuple[SecReportedConcept, ...],
) -> SecUsdResult:
    mapped = False
    matches: list[SecIssuerUsdObservation] = []
    for concept in concepts:
        result = _read_amount(source, period, concept)
        mapped |= result != "unmapped_concept_or_unit"
        if not isinstance(result, str) and source.known_public:
            amount, paths = result
            matches.append(
                SecIssuerUsdObservation(
                    amount=amount,
                    period_start=period.start,
                    period_end=period.end,
                    filing=source.filing,
                    companyfacts_receipt_id=source.capture.receipt_id,
                    companyfacts_received_at=source.received_at,
                    companyfacts_body_sha256=source.capture.body_sha256,
                    fact_paths=paths,
                    concept=concept,
                    publicly_observed_by=source.publicly_observed_by,
                )
            )
    if not source.known_public:
        return "publication_unverified"
    if not mapped:
        return "unmapped_concept_or_unit"
    if not matches:
        return "no_fact_for_scope"
    if len(matches) == 1:
        return matches[0]
    return SecRevenueAmbiguity(tuple(matches))


def _read_amount(
    source: _PinnedCompanyFacts,
    period: SecReportedDuration,
    concept: SecReportedConcept,
) -> tuple[Decimal, tuple[str, ...]] | _MissingAmount:
    tag = concept.removeprefix("us-gaap:")
    if tag not in source.gaap:
        return "unmapped_concept_or_unit"
    entry = _object(source.gaap[tag], f"{tag} concept")
    units = _object(entry.get("units"), f"{tag} units")
    if "USD" not in units:
        return "unmapped_concept_or_unit"
    rows = units["USD"]
    if not isinstance(rows, list):
        raise ValueError("invalid SEC CompanyFacts USD facts")

    label = "revenue" if concept in _REVENUE_CONCEPTS else tag
    # Periods are date-only; UTC+14 bounds the latest possible local civil date.
    latest_civil_date = (source.accepted_at + timedelta(hours=14)).date()
    amount: Decimal | None = None
    paths: list[str] = []
    for index, raw in enumerate(rows):
        row = _object(raw, f"{label} row")
        if _accession(row.get("accn")) != source.filing.accession:
            continue
        if row.get("form") != source.filing.form:
            raise ValueError(f"SEC {label} fact form does not match the filing")
        if source.received_at < source.accepted_at:
            raise ValueError("CompanyFacts fact was received before filing acceptance")
        if source.published_at is not None and source.received_at < source.published_at:
            raise ValueError("CompanyFacts fact was received before witnessed filing publication")
        start, end = _reported_date(row.get("start")), _reported_date(row.get("end"))
        if start > end:
            raise ValueError(f"invalid SEC {label} period")
        if end > latest_civil_date:
            raise ValueError(f"SEC {label} period end is after the filing acceptance date bound")
        if (start, end) != (period.start, period.end):
            continue
        raw_amount = row.get("val")
        if type(raw_amount) not in (int, Decimal):
            raise ValueError(f"SEC {label} value must be a JSON number")
        value = finite_decimal(raw_amount)
        if amount is not None and amount != value:
            raise ValueError(f"conflicting SEC {label} amounts for one accession and period")
        # Keep all locations; total order only breaks equal-value representation ties.
        if amount is None or value.compare_total(amount) < 0:
            amount = value
        paths.append(f"/facts/us-gaap/{tag}/units/USD/{index}")
    if amount is None:
        return "no_fact_for_scope"
    return amount, tuple(paths)


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
    if type(observation.report_end) is not date:
        raise ValueError("invalid SEC index report date")
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
