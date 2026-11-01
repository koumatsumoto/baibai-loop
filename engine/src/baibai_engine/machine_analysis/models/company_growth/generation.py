"""共有読取された固定入力から企業成長を生成する。取得・保存をしない。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from itertools import pairwise

from baibai_engine.machine_analysis.models.company_growth.calculations import (
    build_changes,
    build_period_states,
    build_reported_comparison,
)
from baibai_engine.machine_analysis.models.company_growth.models import (
    CompanyGrowth,
    Coverage,
    InputRelease,
)
from baibai_engine.machine_analysis.models.company_growth.pdf_selection import (
    apply_pdf_changes,
    apply_pdf_periods,
)
from baibai_engine.machine_analysis.observations.financials.edinet_comparisons import (
    FilingComparisons,
)
from baibai_engine.machine_analysis.observations.financials.financial_periods import period_days
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.pdf_selection import (
    select_pdf_financials,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statements import (
    PdfStatement,
    build_pdf_supplement,
)
from baibai_engine.primitives.stocks import normalize_ticker


def generate_company_growth(
    documents: Sequence[JQuantsFinancialSummary],
    *,
    ticker: str,
    sector: str | None,
    as_of: date,
    input_release: InputRelease,
    start: date | None = None,
    end: date | None = None,
    comparisons: Sequence[FilingComparisons] = (),
    comparison_input_reason: str | None = None,
    pdf_statements: Sequence[PdfStatement] = (),
    pdf_input_reason: str | None = None,
) -> CompanyGrowth:
    """期間端で表示を限定し、固定releaseに残る訂正履歴は入力に維持する。"""
    ticker = normalize_ticker(ticker)
    end = end or as_of
    if end > as_of or (start is not None and start > end):
        raise ValueError("company growth requires start <= end <= as_of")
    result = CompanyGrowth(
        ticker=ticker, status="unavailable", requested_start=start, requested_end=end
    )
    selected_pdf = select_pdf_financials(documents, pdf_statements, as_of=as_of)
    states = apply_pdf_periods(
        build_period_states(documents, sector=sector), selected_pdf, sector=sector
    )
    changes = apply_pdf_changes(build_changes(states), selected_pdf, sector=sector)
    observed = tuple(sorted({state.period_end for state in states}))
    displayed = tuple(
        s for s in states if (start is None or start <= s.period_end) and s.period_end <= end
    )
    if any(item.ticker != ticker for item in comparisons):
        raise ValueError("reported comparisons contain a different ticker")
    reported = tuple(
        build_reported_comparison(item)
        for item in comparisons
        if item.source_submit_datetime.date() <= as_of
        and (
            item.current_period_end is None
            or (
                (start is None or start <= item.current_period_end)
                and item.current_period_end <= end
            )
        )
    )
    if any(item.ticker != ticker for item in pdf_statements):
        raise ValueError("PDF statements contain a different ticker")
    pdf = build_pdf_supplement(pdf_statements, as_of=as_of)
    available = bool(displayed or any(c.changes for c in reported) or (pdf and pdf.comparisons))
    return result.model_copy(
        update={
            "status": "available" if available else "unavailable",
            "reason": None if available else "accounting_period_not_observed",
            "reported_comparisons": reported,
            "comparison_input_reason": comparison_input_reason,
            "pdf_supplement": pdf,
            "pdf_selection": selected_pdf,
            "pdf_input_reason": pdf_input_reason,
            "calculation_method": "company_growth_retrospective_v10-pdf-selection"
            if pdf is not None
            else "company_growth_retrospective_v8-edinet-comparisons"
            if reported
            else result.calculation_method,
            "input_release": input_release,
            "periods": displayed,
            "changes": tuple(
                c for c in changes if (start is None or start <= c.to_end) and c.to_end <= end
            ),
            "coverage": Coverage(
                observed_start=observed[0] if observed else None,
                observed_end=observed[-1] if observed else None,
                observed_period_ends=observed,
                gaps_between_observations=tuple(
                    (a, b) for a, b in pairwise(observed) if (b - a).days > 110
                ),
                excluded_document_count=sum(period_days(row) is None for row in documents),
            ),
        }
    )
