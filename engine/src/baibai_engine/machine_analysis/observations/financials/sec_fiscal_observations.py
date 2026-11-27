"""Group SEC fiscal observations by issuer without combining US stock classes."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from baibai_engine.data_collection.providers.sec_edgar import SecEdgarQuery, SecReportedTicker
from baibai_engine.machine_analysis.observations.financials.sec_companyfacts import (
    SecCompanyFactsCapture,
    SecComparativeResult,
    SecFilingWitness,
    SecPublicFilingObservation,
    SecReportedDuration,
    observe_sec_comparatives,
)


@dataclass(frozen=True, slots=True)
class SecFiscalQuarterInput:
    """One selected periodic filing and the exact columns confirmed in that filing."""

    capture: SecCompanyFactsCapture
    filing: SecFilingWitness
    reporting_quarter: str
    current_quarter: SecReportedDuration
    prior_quarter: SecReportedDuration
    current_fiscal_start: date
    prior_fiscal_start: date
    publicly_observed_by: SecPublicFilingObservation | None = None

    def observe(self, *, issuer_cik: str, cutoff: datetime) -> SecComparativeResult:
        return observe_sec_comparatives(
            self.capture,
            self.filing,
            expected_cik=issuer_cik,
            reporting_quarter=self.reporting_quarter,
            current_quarter=self.current_quarter,
            prior_quarter=self.prior_quarter,
            current_fiscal_start=self.current_fiscal_start,
            prior_fiscal_start=self.prior_fiscal_start,
            cutoff=cutoff,
            publicly_observed_by=self.publicly_observed_by,
        )


@dataclass(frozen=True, slots=True)
class SecIssuerFinancialObservations:
    issuer_cik: str
    symbols: tuple[str, ...]
    periodic_period_end: date | None
    periodic_accession: str | None
    periodic: SecComparativeResult | Literal["periodic_source_missing", "invalid_periodic_evidence"]


def observe_sec_issuer_financials(
    subjects: Sequence[SecReportedTicker],
    quarters: Mapping[str, SecFiscalQuarterInput],
    *,
    cutoff: datetime,
) -> tuple[SecIssuerFinancialObservations, ...]:
    """Read selected issuer filings once. Unassociated tickers cannot inherit issuer facts.

    A canonical CIK key with a selected quarter also supports issuer-only analysis;
    it does not establish any association with a stock class.
    """
    if cutoff.utcoffset() is None:
        raise ValueError("SEC financial comparison requires a timezone")
    cutoff = cutoff.astimezone(UTC)
    if any(SecEdgarQuery("companyfacts", issuer).cik != issuer for issuer in quarters):
        raise ValueError("selected SEC quarters must use canonical CIK keys")
    groups: dict[str, set[str]] = {issuer: set() for issuer in quarters}
    symbol_issuers: dict[str, str] = {}
    for subject in subjects:
        if subject.received_at.utcoffset() is None or subject.received_at > cutoff:
            raise ValueError("SEC ticker association must be observed by the cutoff")
        issuer = symbol_issuers.setdefault(subject.symbol, subject.issuer_cik)
        if issuer != subject.issuer_cik:
            raise ValueError("SEC ticker associations disagree on the issuer")
        groups.setdefault(issuer, set()).add(subject.symbol)

    result = []
    for issuer, symbols in sorted(groups.items()):
        source = quarters.get(issuer)
        observation: (
            SecComparativeResult | Literal["periodic_source_missing", "invalid_periodic_evidence"]
        ) = "periodic_source_missing"
        period_end = None
        accession = None
        if source is not None:
            try:
                observation = source.observe(issuer_cik=issuer, cutoff=cutoff)
            except (ValueError, ArithmeticError):
                observation = "invalid_periodic_evidence"
            if isinstance(observation, tuple):
                period_end = source.current_quarter.end
                accession = source.filing.accession
        result.append(
            SecIssuerFinancialObservations(
                issuer, tuple(sorted(symbols)), period_end, accession, observation
            )
        )
    return tuple(result)
