"""保存済みsnapshot間の変化を日次差分viewへ変換する。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal

from baibai_engine.read_api import PortfolioSnapshot, candidate_rows
from baibai_web.readmodel.builders.presentation import _JST, latest_research_by_ticker, number, text
from baibai_web.readmodel.models import (
    CandidateEntryDeltaView,
    CandidateExpectedReturnDeltaView,
    DailyDeltaView,
    DeltaUnavailable,
    HoldingDeltaView,
)
from baibai_web.sources.protocols import (
    LedgerSource,
    MarketPriceSource,
    ResearchSource,
    ScreeningSource,
)
from baibai_web.sources.types import ScreeningRunRecord

_DELTA_ER_MOVERS_SHOWN = 5

_DELTA_ER_MOVE_MIN_PP = 3.0

_DELTA_HOLDING_MOVE_MIN_PCT = 5.0


def build_daily_delta(
    screening: ScreeningSource,
    ledger: LedgerSource,
    research: ResearchSource,
    market: MarketPriceSource,
) -> DailyDeltaView:
    """Compare the latest machine run with the one before it.

    The view exists so that a change does not wait for someone to go looking. It
    reports observations only: which tickers entered or left the candidate list, which
    machine estimates moved, and which holdings had a material observed price move.
    Whether any of that is worth new-stock or holding research is the reader's call.
    Assessments and readings belong to the dedicated Market Context view.

    Sections degrade independently. A store that cannot answer is named in
    ``unavailable`` rather than reported as an empty result, because "no store" and
    "nothing changed" would otherwise look identical.
    """

    now = datetime.now(_JST)
    today = now.date()
    unavailable: list[DeltaUnavailable] = []
    market_ready = market.exists()
    if not market_ready:
        unavailable.append("market")
    latest = screening.latest_run()
    previous = screening.previous_run()
    if latest is None:
        unavailable.append("screening_run")
    elif previous is None:
        unavailable.append("previous_screening_run")

    method_changed = False
    entered: list[CandidateEntryDeltaView] = []
    exited: list[CandidateEntryDeltaView] = []
    er_moves: list[CandidateExpectedReturnDeltaView] = []
    er_moves_total = 0
    if latest is not None and previous is not None:
        method_changed = (
            bool(latest.screening_rules_hash)
            and bool(previous.screening_rules_hash)
            and latest.screening_rules_hash != previous.screening_rules_hash
        )
        comparison = _candidate_comparison(latest, previous)
        method_changed = method_changed or comparison[2]
        if not latest.screening_rules_hash or not previous.screening_rules_hash:
            unavailable.append("candidates")
        elif not method_changed:
            current_entries, previous_entries, _ = comparison
            er_comparable = bool(latest.er_model_version) and (
                latest.er_model_version == previous.er_model_version
            )
            if not er_comparable or any(
                _candidate_er(current_entries[ticker]) is None
                or _candidate_er(previous_entries[ticker]) is None
                for ticker in current_entries.keys() & previous_entries.keys()
            ):
                # Missing either side is unmeasured, not an unchanged estimate.
                # Comparable tickers still contribute their observed movers.
                unavailable.append("candidate_estimate")
            entered, exited, er_moves, er_moves_total = _candidate_deltas(
                current_entries,
                previous_entries,
                market=market if market_ready else None,
                previous_as_of=previous.as_of,
                current_as_of=latest.as_of,
            )
            if not er_comparable:
                # E[r] is context, not membership authority. Keep entries/exits but
                # do not interpret estimates from different models as market moves.
                er_moves, er_moves_total = [], 0

    holdings: list[HoldingDeltaView] = []
    holdings_without_price = 0
    if not ledger.exists():
        unavailable.append("holdings")
    elif market_ready:
        holdings, holdings_without_price = _holding_deltas(
            ledger.snapshot(),
            research=research,
            market=market,
            as_of=today,
            previous_as_of=None if previous is None else previous.as_of,
        )

    return DailyDeltaView(
        generated_at=now,
        as_of=None if latest is None else latest.as_of,
        previous_as_of=None if previous is None else previous.as_of,
        method_changed=method_changed,
        entered=entered,
        exited=exited,
        er_moves=er_moves,
        er_moves_total=er_moves_total,
        holdings=holdings,
        holdings_without_price=holdings_without_price,
        unavailable=sorted(dict.fromkeys(unavailable)),
    )


def _candidate_comparison(
    latest: ScreeningRunRecord, previous: ScreeningRunRecord
) -> tuple[dict[str, Mapping[str, object]], dict[str, Mapping[str, object]], bool]:
    current = {str(row["ticker"]): row for row in candidate_rows(latest.rows)}
    earlier = {str(row["ticker"]): row for row in candidate_rows(previous.rows)}
    latest_method = latest.candidate_discovery["method"]
    previous_method = previous.candidate_discovery["method"]
    if not isinstance(latest_method, Mapping) or not isinstance(previous_method, Mapping):
        raise ValueError("candidate method must be an object")
    hashes = (latest_method.get("method_hash"), previous_method.get("method_hash"))
    if any(not isinstance(value, str) or not value for value in hashes):
        raise ValueError("candidate method hash is unavailable")
    return current, earlier, hashes[0] != hashes[1]


def _candidate_er(row: Mapping[str, object]) -> float | None:
    metrics = row.get("metrics")
    return number(metrics.get("er_annual")) if isinstance(metrics, Mapping) else None


def _candidate_entry_delta(
    row: Mapping[str, object], *, disclosed: bool | None
) -> CandidateEntryDeltaView:
    return CandidateEntryDeltaView(
        ticker=str(row.get("ticker", "")),
        company_name=text(row.get("name")),
        er_annual_pct=_percent(_candidate_er(row)),
        disclosed_since_previous=disclosed,
    )


def _candidate_deltas(
    current: Mapping[str, Mapping[str, object]],
    earlier: Mapping[str, Mapping[str, object]],
    *,
    market: MarketPriceSource | None,
    previous_as_of: date,
    current_as_of: date,
) -> tuple[
    list[CandidateEntryDeltaView],
    list[CandidateEntryDeltaView],
    list[CandidateExpectedReturnDeltaView],
    int,
]:
    entered_tickers = sorted(set(current) - set(earlier))
    exited_tickers = sorted(set(earlier) - set(current))
    # Read only disclosures between the compared runs. A newer market store must
    # not attach later news to an earlier candidate change. This is date context,
    # not a causal claim or proof that the run incorporated the disclosed figures.
    # An absent store leaves this context unknown, not "no disclosure".
    disclosed: set[str] | None = None
    if market is not None:
        disclosed = set(
            market.disclosures_after(
                entered_tickers + exited_tickers,
                after=previous_as_of,
                through_date=current_as_of,
            )
        )
    entered = [
        _candidate_entry_delta(
            current[ticker], disclosed=None if disclosed is None else ticker in disclosed
        )
        for ticker in entered_tickers
    ]
    exited = [
        _candidate_entry_delta(
            earlier[ticker], disclosed=None if disclosed is None else ticker in disclosed
        )
        for ticker in exited_tickers
    ]
    moves: list[tuple[Decimal, CandidateExpectedReturnDeltaView]] = []
    for ticker in sorted(set(current) & set(earlier)):
        current_er = _candidate_er(current[ticker])
        previous_er = _candidate_er(earlier[ticker])
        if current_er is None or previous_er is None:
            continue
        # Decide membership and rank before display rounding. Decimal preserves
        # the stored decimal boundary instead of losing exact 3 pp to subtraction.
        change = (Decimal(str(current_er)) - Decimal(str(previous_er))) * 100
        if abs(change) < _DELTA_ER_MOVE_MIN_PP:
            continue
        moves.append(
            (
                abs(change),
                CandidateExpectedReturnDeltaView(
                    ticker=ticker,
                    company_name=text(current[ticker].get("name")),
                    er_annual_pct=round(current_er * 100, 1),
                    previous_er_annual_pct=round(previous_er * 100, 1),
                    change_pp=float(round(change, 1)),
                ),
            )
        )
    moves.sort(key=lambda item: (-item[0], item[1].ticker))
    return entered, exited, [item for _, item in moves[:_DELTA_ER_MOVERS_SHOWN]], len(moves)


def _holding_deltas(
    snapshot: PortfolioSnapshot,
    *,
    research: ResearchSource,
    market: MarketPriceSource,
    as_of: date,
    previous_as_of: date | None,
) -> tuple[list[HoldingDeltaView], int]:
    tickers = [holding.ticker for holding in snapshot.holdings]
    if not tickers:
        return [], 0
    latest_by_ticker = market.latest_closes(tickers)
    # The change is asked of the market layer so both ends land on the same share
    # basis; dividing two stored closes would report a split as a price move.
    changes = (
        {} if previous_as_of is None else market.close_changes_since(tickers, since=previous_as_of)
    )
    earnings = market.next_earnings_dates(tickers, as_of=as_of)
    revisions = latest_research_by_ticker(research.revisions())
    rows: list[HoldingDeltaView] = []
    without_price = 0
    for holding in snapshot.holdings:
        revision = revisions.get(holding.ticker)
        close = latest_by_ticker.get(holding.ticker)
        if close is None:
            without_price += 1
        change = changes.get(holding.ticker)
        moved = change is not None and abs(change) >= _DELTA_HOLDING_MOVE_MIN_PCT
        if not moved:
            continue
        next_earnings = earnings.get(holding.ticker)
        rows.append(
            HoldingDeltaView(
                ticker=holding.ticker,
                company_name=None if revision is None else revision.company_name,
                change_since_previous_pct=change,
                days_to_next_earnings=(
                    None if next_earnings is None else (next_earnings - as_of).days
                ),
            )
        )
    return rows, without_price


def _percent(value: float | None) -> float | None:
    """Machine E[r] is stored as an annual ratio; the view reports percent."""
    return None if value is None else round(value * 100, 1)
