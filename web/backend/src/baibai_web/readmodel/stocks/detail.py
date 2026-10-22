"""企業detailの読取入力を揃え、各領域のviewを合成する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from baibai_engine.read_api import HoldingSnapshot
from baibai_web.readmodel.builders.presentation import (
    holding_view,
    text,
)
from baibai_web.readmodel.models import (
    InvestmentProposalSummaryView,
    SecurityDetailView,
    SecurityMarketContextView,
)
from baibai_web.readmodel.stocks.presentation import _optional_ledger_snapshot
from baibai_web.readmodel.stocks.research import (
    _assessment_detail_view,
    _proposal_summary_view,
    _research_revision_view,
)
from baibai_web.readmodel.stocks.screening import (
    _JST,
    _screening_run_view,
    _stock_analysis_row_view,
)
from baibai_web.sources.protocols import (
    ExternalAssessmentSource,
    LedgerSource,
    MarketPriceSource,
    ResearchSource,
    ScreeningSource,
)
from baibai_web.sources.types import (
    ResearchRevision,
    ScreeningRunRecord,
)


@dataclass(frozen=True)
class PreparedSecurityInputs:
    """銘柄詳細を見せるための索引を、1回のexportまたはrequest内で共有する。"""

    today: date
    external_cutoff: datetime
    run: ScreeningRunRecord | None
    rows: Mapping[str, Mapping[str, object]]
    revisions: Mapping[str, list[ResearchRevision]]
    proposals: Mapping[str, list[InvestmentProposalSummaryView]]
    holdings: Mapping[str, HoldingSnapshot]
    reserved: set[str]


def prepare_security_inputs(
    ledger: LedgerSource,
    research: ResearchSource,
    screening: ScreeningSource,
) -> PreparedSecurityInputs:
    external_cutoff = datetime.now(_JST)
    today = external_cutoff.date()
    revisions: dict[str, list[ResearchRevision]] = {}
    for revision in research.revisions():
        revisions.setdefault(revision.ticker, []).append(revision)
    proposals: dict[str, list[InvestmentProposalSummaryView]] = {}
    for proposal in research.proposals():
        summary = _proposal_summary_view(proposal)
        items = proposal.get("items")
        if not isinstance(items, list):
            raise ValueError("investment proposal items must be an array")
        for item in items:
            if not isinstance(item, Mapping):
                raise ValueError("investment proposal item must be an object")
            proposals.setdefault(str(item["ticker"]), []).append(summary)
    run = screening.latest_run()
    rows: dict[str, Mapping[str, object]] = {}
    if run is not None:
        for row in run.rows:
            rows.setdefault(str(row.get("ticker", "")), row)
    snapshot = _optional_ledger_snapshot(ledger)
    return PreparedSecurityInputs(
        today=today,
        external_cutoff=external_cutoff,
        run=run,
        rows=rows,
        revisions=revisions,
        proposals=proposals,
        holdings={} if snapshot is None else {item.ticker: item for item in snapshot.holdings},
        reserved=set()
        if snapshot is None
        else {item.ticker for item in snapshot.active_reservations},
    )


def build_security_detail(
    ticker: str,
    ledger: LedgerSource,
    research: ResearchSource,
    screening: ScreeningSource,
    market: MarketPriceSource,
    *,
    prepared: PreparedSecurityInputs | None = None,
    external_assessment: ExternalAssessmentSource | None = None,
) -> SecurityDetailView | None:
    """Build one security page, returning None only when no source knows the ticker."""

    inputs = (
        prepared if prepared is not None else prepare_security_inputs(ledger, research, screening)
    )
    today = inputs.today
    revisions = inputs.revisions.get(ticker, [])
    latest_revision = revisions[0] if revisions else None
    proposals = inputs.proposals.get(ticker, [])
    run = inputs.run
    raw_stock_analysis = inputs.rows.get(ticker)
    holding_snapshot = inputs.holdings.get(ticker)
    assessment = (
        None
        if external_assessment is None
        else external_assessment.assessment(
            ticker,
            cutoff=inputs.external_cutoff,
        )
    )
    has_external = assessment is not None and bool(
        any(source.inputs.latest is not None for source in assessment.model_valuations)
        or any(source.inputs.latest is not None for source in assessment.analyst_ratings)
        or any(source.inputs.latest is not None for source in assessment.quants_scores)
        or assessment.tradingview.inputs.latest is not None
        or assessment.minkabu.latest is not None
    )
    if (
        holding_snapshot is None
        and latest_revision is None
        and raw_stock_analysis is None
        and not has_external
        and not proposals
        and ticker not in inputs.reserved
    ):
        return None

    security_name = text(raw_stock_analysis.get("name")) if raw_stock_analysis is not None else None
    security_sector = (
        text(raw_stock_analysis.get("sector_33")) if raw_stock_analysis is not None else None
    )
    if raw_stock_analysis is None:
        identity = market.security_identities(as_of=today).get(ticker, {})
        security_name = text(identity.get("name")) or None
        security_sector = text(identity.get("sector_33"))
    holding = (
        holding_view(
            holding_snapshot,
            market=market,
            revision=latest_revision,
            security_name=security_name,
            next_earnings_date=market.next_earnings_dates([ticker], as_of=today).get(ticker),
        )
        if holding_snapshot is not None
        else None
    )
    assessment_detail = (
        research.assessment_detail(latest_revision.assessment_id)
        if latest_revision is not None
        else None
    )
    latest_assessment = (
        _assessment_detail_view(assessment_detail) if assessment_detail is not None else None
    )
    reserved_here = {ticker} if ticker in inputs.reserved else set()
    stock_analysis = (
        _stock_analysis_row_view(
            raw_stock_analysis,
            held={ticker} if holding_snapshot is not None else set(),
            reserved=reserved_here,
            researched={ticker} if revisions else set(),
        )
        if raw_stock_analysis is not None
        else None
    )
    return SecurityDetailView(
        market_context=(
            None
            if (context := market.security_market_context(ticker, as_of=today)) is None
            else SecurityMarketContextView.model_validate(context)
        ),
        external_assessment=assessment,
        ticker=ticker,
        company_name=(
            latest_revision.company_name if latest_revision is not None else security_name
        ),
        sector=(
            latest_revision.sector
            if latest_revision is not None
            else security_sector
            or (holding_snapshot.sector if holding_snapshot is not None else None)
        ),
        holding=holding,
        revisions=[_research_revision_view(item) for item in revisions],
        latest_assessment=latest_assessment,
        investment_proposals=proposals,
        stock_analysis=stock_analysis,
        screening_run=(_screening_run_view(run, today=today) if run is not None else None),
    )
