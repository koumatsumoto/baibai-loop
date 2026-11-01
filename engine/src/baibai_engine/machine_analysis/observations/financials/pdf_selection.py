"""保持開示の最新期・訂正を先に選び、PDF組替比較を同一基準で採用する。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    accounting_period_identity,
    accounting_standard,
    certainly_disclosed_by,
    latest_disclosure_candidates,
    project_financial_documents,
    reporting_basis_rows,
    statement_basis,
)
from baibai_engine.machine_analysis.observations.financials.financial_periods import (
    is_annual_period,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statements import (
    Metric,
    PdfStatement,
)

type AnnualRevisionBasis = Literal["same_disclosure", "latest_reconfirmed_unchanged_amounts"]

REVISION = "retained-pdf-restatement-selection-v1"
_FIELDS: dict[Metric, str] = {
    "sales": "sales",
    "operating_profit": "operating_profit",
    "parent_profit": "profit",
}


class PdfFinancialSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    revision: str = REVISION
    status: Literal["available", "unavailable"]
    reason: str | None = None
    blocks_legacy_ttm: bool = False
    comparative_verified: bool = True
    read_semantics: Literal["latest_within_retained_disclosures"] = (
        "latest_within_retained_disclosures"
    )
    precision: Literal["derived_from_truncated_display_amounts"] = (
        "derived_from_truncated_display_amounts"
    )
    period_start: date | None = None
    period_end: date | None = None
    current: PdfStatement | None = None
    annual: PdfStatement | None = None
    annual_latest_disclosure_number: str | None = None
    annual_revision_basis: AnnualRevisionBasis | None = None
    amounts_yen: dict[Metric, int] = {}


def _latest(rows: Sequence[JQuantsFinancialSummary]) -> JQuantsFinancialSummary | None:
    candidates = latest_disclosure_candidates(rows)
    identities = {(r.disclosure_number, r.document_type) for r in candidates}
    if len(identities) != 1:
        return None
    # Equal native IDs with conflicting payloads cannot be ordered by input order.
    signatures = {
        (
            r.period_start,
            r.period_end,
            r.sales,
            r.operating_profit,
            r.profit,
            r.unresolved_actual_fields,
        )
        for r in candidates
    }
    return candidates[0] if len(signatures) == 1 else None


def _matching_pdf(
    row: JQuantsFinancialSummary, statements: Sequence[PdfStatement]
) -> tuple[PdfStatement | None, str | None]:
    candidates = {
        item.source_sha256: item
        for item in statements
        if item.disclosure_number == row.disclosure_number
    }
    if len(candidates) != 1:
        return None, "latest_disclosure_pdf_missing_or_ambiguous"
    item = next(iter(candidates.values()))
    if item.reason:
        return None, item.reason
    if (
        item.published_on != row.disclosed_at
        or (item.current_start, item.current_end) != (row.period_start, row.period_end)
        or item.accounting_standard != accounting_standard(row)
        or item.statement_basis != statement_basis(row)
        or item.unit != "JPY_million"
        or item.rounding != "truncate_below_unit"
    ):
        return None, "pdf_disclosure_identity_or_basis_conflict"
    if {pair.metric for pair in item.pairs} != set(_FIELDS):
        return None, "pdf_metrics_incomplete"
    for pair in item.pairs:
        field = _FIELDS[pair.metric]
        if field in row.unresolved_actual_fields:
            return None, "latest_disclosure_metric_conflict:" + field
        precise = getattr(row, field)
        if precise is not None and int(Decimal(str(precise)) / 1_000_000) != pair.current:
            return None, "pdf_precise_amount_conflict:" + field
    return item, None


def _unchanged_annual_pdf(
    latest: JQuantsFinancialSummary,
    rows: Sequence[JQuantsFinancialSummary],
    statements: Sequence[PdfStatement],
) -> PdfStatement | None:
    # Only the annual CURRENT amounts are used. A current quarter's comparative
    # column can change while its current amounts stay unchanged, so never use
    # this rule for that operand.
    if any(item.disclosure_number == latest.disclosure_number for item in statements):
        return None
    fields = tuple(_FIELDS.values())
    if any(
        getattr(latest, field) is None or field in latest.unresolved_actual_fields
        for field in fields
    ):
        return None
    candidates = {}
    for row in rows:
        if (
            row.disclosure_number == latest.disclosure_number
            or (row.period_start, row.period_end) != (latest.period_start, latest.period_end)
            or accounting_standard(row) != accounting_standard(latest)
            or statement_basis(row) != statement_basis(latest)
            or not certainly_disclosed_by(row, latest)
            or any(
                getattr(row, field) != getattr(latest, field)
                or field in row.unresolved_actual_fields
                for field in fields
            )
        ):
            continue
        item, _ = _matching_pdf(row, statements)
        if item is not None:
            candidates[item.source_sha256] = item
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def select_pdf_financials(
    documents: Sequence[JQuantsFinancialSummary], statements: Sequence[PdfStatement], *, as_of: date
) -> PdfFinancialSelection | None:
    if not statements:
        return None
    tickers = {item.ticker for item in statements} | {row.ticker for row in documents}
    if len(tickers) != 1:
        raise ValueError("PDF financial selection requires one issuer")
    retained = [
        item for item in statements if item.published_on is None or item.published_on <= as_of
    ]
    if not retained:
        return None
    rows = [
        row
        for row in reporting_basis_rows(project_financial_documents(documents, as_of))
        if row.period_start is not None
        and row.period_end is not None
        and row.period_start <= row.period_end <= as_of
    ]
    anchor = max((accounting_period_identity(row) for row in rows), default=None)
    current_row = _latest([row for row in rows if accounting_period_identity(row) == anchor])
    if current_row is None:
        return PdfFinancialSelection(
            status="unavailable", reason="latest_disclosure_missing_or_ambiguous"
        )
    current, reason = _matching_pdf(current_row, retained)
    current_is_annual = (
        current_row.period_start is not None
        and current_row.period_end is not None
        and is_annual_period(current_row.period_start, current_row.period_end)
    )
    if current is None and current_is_annual:
        current = _unchanged_annual_pdf(current_row, rows, retained)
    if current is None:
        return PdfFinancialSelection(
            status="unavailable",
            reason=reason,
            period_start=current_row.period_start,
            period_end=current_row.period_end,
            blocks_legacy_ttm=any(
                (item.current_start, item.current_end)
                == (current_row.period_start, current_row.period_end)
                for item in retained
            ),
        )
    assert current.current_start is not None
    assert current.current_end is not None
    if is_annual_period(current.current_start, current.current_end):
        return PdfFinancialSelection(
            status="available",
            current=current,
            comparative_verified=current.disclosure_number == current_row.disclosure_number,
            annual_latest_disclosure_number=current_row.disclosure_number,
            annual_revision_basis="same_disclosure"
            if current.disclosure_number == current_row.disclosure_number
            else "latest_reconfirmed_unchanged_amounts",
            period_start=current.current_start,
            period_end=current.current_end,
            amounts_yen={pair.metric: pair.current * 1_000_000 for pair in current.pairs},
        )
    annual_row = _latest(
        [
            row
            for row in rows
            if row.period_start is not None
            and row.period_end is not None
            and row.period_start == current.comparative_start
            and row.period_end == current.current_start - timedelta(days=1)
            and is_annual_period(row.period_start, row.period_end)
        ]
    )
    if annual_row is None:
        return PdfFinancialSelection(
            status="unavailable", current=current, reason="annual_disclosure_missing_or_ambiguous"
        )
    annual, reason = _matching_pdf(annual_row, retained)
    annual_revision_basis: AnnualRevisionBasis = "same_disclosure"
    if annual is None:
        annual = _unchanged_annual_pdf(annual_row, rows, retained)
        annual_revision_basis = "latest_reconfirmed_unchanged_amounts"
    if annual is None:
        return PdfFinancialSelection(status="unavailable", current=current, reason=reason)
    if (
        annual.published_on is None
        or current.published_on is None
        or annual.published_on > current.published_on
    ):
        return PdfFinancialSelection(
            status="unavailable",
            current=current,
            annual=annual,
            reason="annual_revised_after_current_disclosure",
        )
    if current.comparative_end is None:
        return PdfFinancialSelection(
            status="unavailable", current=current, reason="comparative_period_missing"
        )
    start = current.comparative_end + timedelta(days=1)
    if not is_annual_period(start, current.current_end):
        return PdfFinancialSelection(
            status="unavailable", current=current, reason="ttm_period_not_annual"
        )
    annual_values = {pair.metric: pair.current for pair in annual.pairs}
    return PdfFinancialSelection(
        status="available",
        current=current,
        annual=annual,
        annual_latest_disclosure_number=annual_row.disclosure_number,
        annual_revision_basis=annual_revision_basis,
        period_start=start,
        period_end=current.current_end,
        amounts_yen={
            pair.metric: (annual_values[pair.metric] + pair.current - pair.comparative) * 1_000_000
            for pair in current.pairs
        },
    )
