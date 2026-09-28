"""企業detailの読取入力を揃え、各領域のviewを合成する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from baibai_engine.read_api import HoldingSnapshot
from baibai_web.readmodel.builders.presentation import holding_view, text
from baibai_web.readmodel.models import (
    PositionReviewView,
    ReviewSetEntryView,
    SecurityDetailView,
)
from baibai_web.readmodel.stocks.presentation import _safe_snapshot
from baibai_web.readmodel.stocks.research import _research_revision_view, _thesis_detail_view
from baibai_web.readmodel.stocks.screening import (
    _JST,
    _fair_value_by_ticker,
    _operative_run,
    _screening_run_view,
    _security_analysis_row_view,
)
from baibai_web.sources.protocols import (
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
    run: ScreeningRunRecord | None
    rows: Mapping[str, Mapping[str, object]]
    revisions: Mapping[str, list[ResearchRevision]]
    holdings: Mapping[str, HoldingSnapshot]
    reserved: set[str]
    fair_value: Mapping[str, ReviewSetEntryView]


def prepare_security_inputs(
    ledger: LedgerSource,
    research: ResearchSource,
    screening: ScreeningSource,
) -> PreparedSecurityInputs:
    today = datetime.now(_JST).date()
    revisions: dict[str, list[ResearchRevision]] = {}
    for revision in research.revisions():
        revisions.setdefault(revision.ticker, []).append(revision)
    run, review_sets = _operative_run(screening)
    rows: dict[str, Mapping[str, object]] = {}
    if run is not None:
        for row in run.rows:
            rows.setdefault(str(row.get("ticker", "")), row)
    snapshot = _safe_snapshot(ledger)
    return PreparedSecurityInputs(
        today=today,
        run=run,
        rows=rows,
        revisions=revisions,
        holdings={} if snapshot is None else {item.ticker: item for item in snapshot.holdings},
        reserved=set()
        if snapshot is None
        else {item.ticker for item in snapshot.active_reservations},
        fair_value=_fair_value_by_ticker(review_sets),
    )


def build_security_detail(
    ticker: str,
    ledger: LedgerSource,
    research: ResearchSource,
    screening: ScreeningSource,
    market: MarketPriceSource,
    *,
    prepared: PreparedSecurityInputs | None = None,
) -> SecurityDetailView | None:
    """Build one security page, returning None only when no source knows the ticker."""

    inputs = (
        prepared if prepared is not None else prepare_security_inputs(ledger, research, screening)
    )
    today = inputs.today
    revisions = inputs.revisions.get(ticker, [])
    latest_revision = revisions[0] if revisions else None
    run = inputs.run
    raw_security_analysis = inputs.rows.get(ticker)
    holding_snapshot = inputs.holdings.get(ticker)
    if holding_snapshot is None and latest_revision is None and raw_security_analysis is None:
        return None

    security_name = (
        text(raw_security_analysis.get("name")) if raw_security_analysis is not None else None
    )
    security_sector = (
        text(raw_security_analysis.get("sector_33")) if raw_security_analysis is not None else None
    )
    holding = (
        holding_view(
            holding_snapshot,
            revision=latest_revision,
            security_name=security_name,
            next_earnings_date=market.next_earnings_dates([ticker], as_of=today).get(ticker),
        )
        if holding_snapshot is not None
        else None
    )
    latest_thesis = (
        _thesis_detail_view(research.thesis_detail(latest_revision.thesis_id))
        if latest_revision is not None
        else None
    )
    reserved_here = {ticker} if ticker in inputs.reserved else set()
    security_analysis = (
        _security_analysis_row_view(
            raw_security_analysis,
            held={ticker} if holding_snapshot is not None else set(),
            reserved=reserved_here,
            researched={ticker} if revisions else set(),
            fair_value=inputs.fair_value,
        )
        if raw_security_analysis is not None
        else None
    )
    return SecurityDetailView(
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
        latest_thesis=latest_thesis,
        position_reviews=[
            PositionReviewView(
                position_review_id=item.position_review_id,
                as_of=item.as_of,
                thesis_id=item.thesis_id,
                action=item.action,
                note=item.note,
            )
            for item in research.position_reviews(ticker=ticker)
        ],
        security_analysis=security_analysis,
        screening_run=(_screening_run_view(run, today=today) if run is not None else None),
    )
