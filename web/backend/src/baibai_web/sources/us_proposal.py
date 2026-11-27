"""同じAppスナップショットから米国株の正式判断を一件だけ投影する。"""

import sqlite3
from datetime import datetime

from baibai_engine.investment_judgment.us_proposal import USProposal, load_latest_us_proposal
from baibai_web.readmodel.us_proposal_models import (
    LatestUSProposalView,
    USProposalComparisonScenarioView,
    USProposalComparisonView,
    USProposalIexBandView,
    USProposalOfficialCloseView,
    USProposalView,
)


def _project(record: USProposal) -> USProposalView:
    decision, analysis = record.decision, record.analysis.draft
    close, band, comparison = analysis.official_close, analysis.iex_band, analysis.comparison
    comparison_view = None
    if comparison is not None:
        assert close is not None
        base, downside = (
            USProposalComparisonScenarioView(
                terminal_value_usd_per_share=scenario.terminal_value_usd_per_share,
                eligible_distributions_usd_per_share=scenario.eligible_distributions_usd_per_share,
                difference_from_cash_usd_per_share=scenario.difference_from_cash(close.price_usd),
            )
            for scenario in (comparison.base, comparison.downside)
        )
        comparison_view = USProposalComparisonView(
            cash_basis=comparison.cash_basis, base=base, downside=downside
        )
    return USProposalView(
        proposal_id=decision.proposal_id,
        draft_sha256=record.review.draft_sha256,
        ticker=analysis.ticker,
        venue=analysis.venue,
        share_class=analysis.share_class,
        currency=analysis.currency,
        session_et=analysis.expected_session_et,
        cutoff_at=analysis.cutoff_at,
        published_at=record.published_at,
        reviewed_at=record.review.reviewed_at,
        action=decision.action,
        reason=decision.reason,
        revisit_when=decision.revisit_when,
        official_close=None
        if close is None
        else USProposalOfficialCloseView(
            price_usd=close.price_usd, origin="listing_exchange", observed_at=close.observed_at
        ),
        iex_band=None
        if band is None
        else USProposalIexBandView(
            first_session_et=band.first_session_et,
            last_session_et=band.last_session_et,
            observations=band.observations,
            low_usd=band.low_usd,
            high_usd=band.high_usd,
            last_usd=band.last_usd,
        ),
        comparison_horizon_et=None if comparison is None else comparison.horizon_et,
        comparison=comparison_view,
        unresolved=list(analysis.unresolved),
    )


def unavailable_us_proposal(generated_at: datetime) -> LatestUSProposalView:
    return LatestUSProposalView(
        generated_at=generated_at,
        status="unavailable",
        error="保存済みの米国株判断を読み取れません。",
        proposal=None,
    )


def read_us_proposal_in_snapshot(
    connection: sqlite3.Connection, *, generated_at: datetime
) -> LatestUSProposalView:
    try:
        saved = load_latest_us_proposal(connection)
        view = None if saved is None else _project(saved)
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        # Stored content, App paths and provider metadata do not belong in a serving error.
        return unavailable_us_proposal(generated_at)
    return LatestUSProposalView(
        generated_at=generated_at,
        status="empty" if view is None else "ready",
        error=None,
        proposal=view,
    )
