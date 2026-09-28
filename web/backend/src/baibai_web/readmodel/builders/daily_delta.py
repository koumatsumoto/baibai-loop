"""保存済みsnapshot間の変化を日次差分viewへ変換する。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime

from baibai_engine.read_api import PortfolioSnapshot
from baibai_web.readmodel.builders.presentation import _JST, latest_research_by_ticker, number, text
from baibai_web.readmodel.models import (
    DailyDeltaView,
    DeltaUnavailable,
    HoldingDeltaView,
    ReviewSetEntryDeltaView,
    ReviewSetExpectedReturnDeltaView,
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
    reports observations only: which tickers entered or left the Review Set, which
    machine estimates moved, and which holdings stand at or above their recorded fair
    value. Whether any of that is worth a research cycle or a Position Review is the
    reader's call. Macro context and readings belong to the dedicated Macro view.

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
    entered: list[ReviewSetEntryDeltaView] = []
    exited: list[ReviewSetEntryDeltaView] = []
    er_moves: list[ReviewSetExpectedReturnDeltaView] = []
    er_moves_total = 0
    if latest is not None and previous is not None:
        method_changed = (
            bool(latest.screening_rules_hash)
            and bool(previous.screening_rules_hash)
            and latest.screening_rules_hash != previous.screening_rules_hash
        )
        comparison = _review_set_comparison(screening, latest, previous)
        if comparison is not None:
            method_changed = method_changed or comparison[2]
        if (
            comparison is None
            or not latest.screening_rules_hash
            or not previous.screening_rules_hash
        ):
            unavailable.append("review_set")
        elif not method_changed:
            current_entries, previous_entries, _ = comparison
            er_comparable = bool(latest.er_model_version) and (
                latest.er_model_version == previous.er_model_version
            )
            if not er_comparable or any(
                _review_set_er(current_entries[ticker]) is None
                or _review_set_er(previous_entries[ticker]) is None
                for ticker in current_entries.keys() & previous_entries.keys()
            ):
                # Missing either side is unmeasured, not an unchanged estimate.
                # Comparable tickers still contribute their observed movers.
                unavailable.append("review_set_estimate")
            entered, exited, er_moves, er_moves_total = _review_set_deltas(
                current_entries,
                previous_entries,
                market=market if market_ready else None,
                previous_as_of=previous.as_of,
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


def _review_set_rows(
    payloads: list[dict[str, object]],
) -> tuple[str, dict[str, Mapping[str, object]]] | None:
    """Read a published Review Set's method hash and entries indexed by ticker."""

    for payload in payloads:
        body = payload.get("payload")
        rows = body.get("entries") if isinstance(body, Mapping) else None
        if not isinstance(rows, list):
            continue
        method = body.get("method") if isinstance(body, Mapping) else None
        method_hash = text(method.get("method_hash")) if isinstance(method, Mapping) else None
        if not method_hash:
            return None
        indexed = {
            str(row.get("ticker")): row
            for row in rows
            if isinstance(row, Mapping) and row.get("ticker")
        }
        if len(indexed) != len(rows):
            return None
        return method_hash, indexed
    return None


def _review_set_comparison(
    screening: ScreeningSource, latest: ScreeningRunRecord, previous: ScreeningRunRecord
) -> tuple[dict[str, Mapping[str, object]], dict[str, Mapping[str, object]], bool] | None:
    """Resolve both published Review Sets and whether their Discovery methods differ.

    Review Set membership is published by Candidate Discovery; re-deriving it from
    the run's Security Analysis array would duplicate that method and compare the
    whole universe.
    """

    latest_payloads = screening.review_sets(run_revision_id=latest.run_revision_id)
    previous_payloads = screening.review_sets(run_revision_id=previous.run_revision_id)
    current = _review_set_rows(latest_payloads)
    earlier = _review_set_rows(previous_payloads)
    if current is None or earlier is None:
        return None
    return current[1], earlier[1], current[0] != earlier[0]


def _review_set_er(row: Mapping[str, object]) -> float | None:
    """Read contextual E[r] from a Review Set entry."""

    analysis = row.get("analysis")
    expected = analysis.get("expected_return") if isinstance(analysis, Mapping) else None
    return number(expected.get("er_annual")) if isinstance(expected, Mapping) else None


def _review_set_entry_delta(
    row: Mapping[str, object], *, disclosed: bool | None
) -> ReviewSetEntryDeltaView:
    return ReviewSetEntryDeltaView(
        ticker=str(row.get("ticker", "")),
        company_name=text(row.get("name")),
        er_annual_pct=_percent(_review_set_er(row)),
        disclosed_since_previous=disclosed,
    )


def _review_set_deltas(
    current: Mapping[str, Mapping[str, object]],
    earlier: Mapping[str, Mapping[str, object]],
    *,
    market: MarketPriceSource | None,
    previous_as_of: date,
) -> tuple[
    list[ReviewSetEntryDeltaView],
    list[ReviewSetEntryDeltaView],
    list[ReviewSetExpectedReturnDeltaView],
    int,
]:
    entered_tickers = sorted(set(current) - set(earlier))
    exited_tickers = sorted(set(earlier) - set(current))
    # A name that reported between the two runs entered on new numbers, not on a price
    # move alone. The review_set output carries no disclosure date, so it is read from
    # the market store for the tickers that actually changed side. When that store
    # cannot answer, the fact stays unknown instead of reading as "no disclosure".
    disclosed: set[str] | None = None
    if market is not None:
        disclosed = set(
            market.disclosures_after(entered_tickers + exited_tickers, after=previous_as_of)
        )
    entered = [
        _review_set_entry_delta(
            current[ticker], disclosed=None if disclosed is None else ticker in disclosed
        )
        for ticker in entered_tickers
    ]
    exited = [
        _review_set_entry_delta(
            earlier[ticker], disclosed=None if disclosed is None else ticker in disclosed
        )
        for ticker in exited_tickers
    ]
    moves: list[ReviewSetExpectedReturnDeltaView] = []
    for ticker in sorted(set(current) & set(earlier)):
        current_er = _percent(_review_set_er(current[ticker]))
        previous_er = _percent(_review_set_er(earlier[ticker]))
        if current_er is None or previous_er is None:
            continue
        change = round(current_er - previous_er, 1)
        if abs(change) < _DELTA_ER_MOVE_MIN_PP:
            continue
        moves.append(
            ReviewSetExpectedReturnDeltaView(
                ticker=ticker,
                company_name=text(current[ticker].get("name")),
                er_annual_pct=current_er,
                previous_er_annual_pct=previous_er,
                change_pp=change,
            )
        )
    moves.sort(key=lambda item: (-abs(item.change_pp), item.ticker))
    return entered, exited, moves[:_DELTA_ER_MOVERS_SHOWN], len(moves)


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
