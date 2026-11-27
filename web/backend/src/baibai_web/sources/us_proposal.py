"""同じAppスナップショットから米国株の正式判断と直近履歴を投影する。"""

import sqlite3
from datetime import datetime

from baibai_engine.investment_judgment.us_proposal import (
    USProposal,
    load_latest_us_proposal,
    load_us_proposal,
    published_us_proposal_ids,
)
from baibai_web.readmodel.us_proposal_models import (
    LatestUSProposalView,
    USProposalComparisonScenarioView,
    USProposalComparisonView,
    USProposalHistoryView,
    USProposalIexBandView,
    USProposalOfficialCloseView,
    USProposalView,
)

_HISTORY_LIMIT = 20
_READ_ERRORS = (OSError, ValueError, RuntimeError, sqlite3.Error)
_LATEST_ERROR = "保存済みの米国株判断を読み取れません。"
_HISTORY_ERROR = "保存済みの米国株判断の履歴を読み取れません。"


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
            price_usd=close.price_usd, origin=close.origin, observed_at=close.observed_at
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
        error=_LATEST_ERROR,
        proposal=None,
        history=_unavailable_history(),
    )


def _unavailable_history() -> USProposalHistoryView:
    return USProposalHistoryView(
        status="unavailable",
        error=_HISTORY_ERROR,
        proposals=[],
        unavailable_proposal_ids=[],
        has_more=None,
    )


def _read_history(
    connection: sqlite3.Connection, latest: USProposalView | None
) -> USProposalHistoryView:
    try:
        slots = published_us_proposal_ids(connection, limit=_HISTORY_LIMIT + 1)
    except _READ_ERRORS:
        return _unavailable_history()
    if latest is not None and (not slots or slots[0] != latest.proposal_id):
        return _unavailable_history()

    unreadable = False
    proposals = []
    missing = []
    for proposal_id in slots[:_HISTORY_LIMIT]:
        if proposal_id is None:
            unreadable = True
            continue
        if latest is not None and proposal_id == latest.proposal_id:
            continue
        try:
            saved = load_us_proposal(connection, proposal_id)
            if saved is not None:
                proposals.append(_project(saved))
                continue
        except _READ_ERRORS:
            pass
        unreadable = True
        missing.append(proposal_id)

    readable = latest is not None or bool(proposals)
    return USProposalHistoryView(
        status=("partial" if readable else "unavailable")
        if unreadable
        else ("ready" if readable else "empty"),
        error=("一部の保存済みの米国株判断の履歴を読み取れません。" if readable else _HISTORY_ERROR)
        if unreadable
        else None,
        proposals=proposals,
        unavailable_proposal_ids=missing,
        has_more=len(slots) > _HISTORY_LIMIT,
    )


def read_us_proposal_in_snapshot(
    connection: sqlite3.Connection, *, generated_at: datetime
) -> LatestUSProposalView:
    try:
        saved = load_latest_us_proposal(connection)
        view = None if saved is None else _project(saved)
        error = None
    except _READ_ERRORS:
        # Stored content, App paths and provider metadata do not belong in a serving error.
        view, error = None, _LATEST_ERROR
    return LatestUSProposalView(
        generated_at=generated_at,
        status="unavailable" if error is not None else "empty" if view is None else "ready",
        error=error,
        proposal=view,
        history=_read_history(connection, view),
    )
