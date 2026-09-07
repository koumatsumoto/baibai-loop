"""Show cross-page operational state through the core Web read models."""

from __future__ import annotations

from collections.abc import (
    Mapping,
)
from datetime import (
    date,
    datetime,
    timedelta,
)
from decimal import (
    Decimal,
    InvalidOperation,
)
from zoneinfo import (
    ZoneInfo,
)

from baibai_engine.read_api import (
    HoldingSnapshot,
    PortfolioLedgerError,
    PortfolioSnapshot,
)
from baibai_web.sources.db_sources import (
    DbMetaSource,
    DbOperationsSource,
)
from baibai_web.sources.protocols import (
    LedgerSource,
    MarketPriceSource,
    ResearchSource,
    ScreeningSource,
    TaskSource,
)
from baibai_web.sources.types import (
    ResearchRevision,
    ScreeningRunRecord,
    TaskRecord,
)

from .models import (
    DailyDeltaView,
    DashboardView,
    DeltaUnavailable,
    HoldingDeltaView,
    HoldingView,
    MetaBatch,
    MetaView,
    OperationSessionView,
    OperationsView,
    PortfolioOutcomeView,
    ReservationView,
    ReviewSetEntryDeltaView,
    ReviewSetExpectedReturnDeltaView,
    TasksView,
    TaskView,
    UpcomingEventView,
    WarningView,
)

_EVENT_WINDOW_DAYS = 14


_EVENT_KIND_ORDER = {"earnings": 0, "reservation_expiry": 1}


_JST = ZoneInfo("Asia/Tokyo")


def build_meta(source: DbMetaSource, *, batch: MetaBatch | None = None) -> MetaView:
    """Report per-store as-of freshness so a consumer can judge view staleness."""

    return MetaView(
        generated_at=datetime.now(_JST),
        data_updated_at=source.data_updated_at(),
        screening_as_of=source.screening_as_of(),
        macro_as_of=source.macro_as_of(),
        app_db_updated_at=source.app_db_updated_at(),
        batch=batch,
    )


def build_operations_view(source: DbOperationsSource) -> OperationsView:
    return OperationsView(
        operations=[OperationSessionView.model_validate(item) for item in source.operations()],
        outcomes=[PortfolioOutcomeView.model_validate(item) for item in source.outcomes()],
    )


def build_dashboard(
    ledger: LedgerSource,
    research: ResearchSource,
    screening: ScreeningSource,
    market: MarketPriceSource,
) -> DashboardView:
    """Build the app's first view without performing storage I/O directly."""

    now = datetime.now(_JST)
    today = now.date()
    revisions = research.revisions()
    latest_research = _latest_research_by_ticker(revisions)
    latest_run = screening.latest_run()
    security_names = _security_names(latest_run)
    research_load_errors = research.load_errors()

    if not ledger.exists():
        return _empty_dashboard(
            generated_at=now,
            ledger_exists=False,
            ledger_error=None,
            research_load_errors=research_load_errors,
        )
    try:
        snapshot = ledger.snapshot()
    except PortfolioLedgerError as error:
        return _empty_dashboard(
            generated_at=now,
            ledger_exists=True,
            ledger_error=str(error),
            research_load_errors=research_load_errors,
        )

    holding_tickers = [holding.ticker for holding in snapshot.holdings]
    earnings_dates = market.next_earnings_dates(holding_tickers, as_of=today)
    holdings = [
        _holding_view(
            holding,
            revision=latest_research.get(holding.ticker),
            security_name=security_names.get(holding.ticker),
            next_earnings_date=earnings_dates.get(holding.ticker),
        )
        for holding in snapshot.holdings
    ]
    holdings.sort(
        key=lambda item: (item.market_value_yen is not None, item.market_value_yen or 0),
        reverse=True,
    )
    reservations = [
        ReservationView(
            reservation_id=item.reservation_id,
            ticker=item.ticker,
            sector=item.sector,
            remaining_quantity=item.remaining_quantity,
            price_guard_yen=str(item.price_guard_yen),
            reserved_yen=item.reserved_yen,
            expires_at=item.expires_at,
        )
        for item in snapshot.active_reservations
    ]
    reservations.sort(key=lambda item: item.expires_at)
    warnings = [
        WarningView(
            code=item.code,
            scope=item.scope,
            key=item.key,
            actual_pct=item.actual_pct,
            warning_pct=item.warning_pct,
            overridden=item.overridden,
        )
        for item in snapshot.warnings
    ]
    holdings_market_value = snapshot.holdings_market_value_yen
    available_cash = snapshot.available_cash_yen
    reserved_cash = snapshot.reserved_cash_yen
    total = snapshot.total_capital_yen
    valuation_as_of = (
        None
        if any(item.market_price_as_of is None for item in holdings)
        else min(
            (item.market_price_as_of for item in holdings if item.market_price_as_of is not None),
            default=snapshot.as_of,
        )
    )
    return DashboardView(
        generated_at=now,
        ledger_exists=True,
        ledger_error=None,
        ledger_as_of=snapshot.as_of,
        ledger_stale=snapshot.as_of.date() <= today - timedelta(days=7),
        valuation_as_of=valuation_as_of,
        valuation_stale=valuation_as_of is None
        or valuation_as_of.date() <= today - timedelta(days=7),
        total_capital_yen=total,
        available_cash_yen=available_cash,
        reserved_cash_yen=reserved_cash,
        holdings_market_value_yen=holdings_market_value,
        deployed_cost_yen=snapshot.deployed_cost_yen,
        cash_pct=_percentage(available_cash, total, digits=1),
        reserved_pct=_percentage(reserved_cash, total, digits=1),
        deployed_pct=_percentage(holdings_market_value, total, digits=1),
        holdings=holdings,
        reservations=reservations,
        warnings=warnings,
        research_load_errors=research_load_errors,
    )


def build_tasks(
    tasks: TaskSource,
    ledger: LedgerSource,
    research: ResearchSource,
    screening: ScreeningSource,
    market: MarketPriceSource,
) -> TasksView:
    """Build task workflow independently from Dashboard portfolio presentation."""

    now = datetime.now(_JST)
    today = now.date()
    open_tasks, next_task = _task_views(tasks.list_tasks(), today=today)
    tasks_exist = tasks.exists()
    if not ledger.exists():
        return TasksView(
            generated_at=now,
            tasks_exist=tasks_exist,
            open_tasks=open_tasks,
            next_task=next_task,
            upcoming_events=[],
            ledger_error=None,
        )
    try:
        snapshot = ledger.snapshot()
    except PortfolioLedgerError as error:
        return TasksView(
            generated_at=now,
            tasks_exist=tasks_exist,
            open_tasks=open_tasks,
            next_task=next_task,
            upcoming_events=[],
            ledger_error=str(error),
        )

    latest_research = _latest_research_by_ticker(research.revisions())
    security_names = _security_names(screening.latest_run())
    holding_tickers = [holding.ticker for holding in snapshot.holdings]
    earnings_dates = market.next_earnings_dates(holding_tickers, as_of=today)
    holdings = [
        _holding_view(
            holding,
            revision=latest_research.get(holding.ticker),
            security_name=security_names.get(holding.ticker),
            next_earnings_date=earnings_dates.get(holding.ticker),
        )
        for holding in snapshot.holdings
    ]
    reservations = [
        ReservationView(
            reservation_id=item.reservation_id,
            ticker=item.ticker,
            sector=item.sector,
            remaining_quantity=item.remaining_quantity,
            price_guard_yen=str(item.price_guard_yen),
            reserved_yen=item.reserved_yen,
            expires_at=item.expires_at,
        )
        for item in snapshot.active_reservations
    ]
    return TasksView(
        generated_at=now,
        tasks_exist=tasks_exist,
        open_tasks=open_tasks,
        next_task=next_task,
        upcoming_events=_upcoming_events(
            today=today,
            holdings=holdings,
            reservations=reservations,
        ),
        ledger_error=None,
    )


def _empty_dashboard(
    *,
    generated_at: datetime,
    ledger_exists: bool,
    ledger_error: str | None,
    research_load_errors: list[str],
) -> DashboardView:
    return DashboardView(
        generated_at=generated_at,
        ledger_exists=ledger_exists,
        ledger_error=ledger_error,
        ledger_as_of=None,
        ledger_stale=False,
        valuation_as_of=None,
        valuation_stale=False,
        total_capital_yen=None,
        available_cash_yen=None,
        reserved_cash_yen=None,
        holdings_market_value_yen=None,
        deployed_cost_yen=None,
        cash_pct=None,
        reserved_pct=None,
        deployed_pct=None,
        holdings=[],
        reservations=[],
        warnings=[],
        research_load_errors=research_load_errors,
    )


def _latest_research_by_ticker(
    revisions: list[ResearchRevision],
) -> dict[str, ResearchRevision]:
    latest: dict[str, ResearchRevision] = {}
    for revision in revisions:
        latest.setdefault(revision.ticker, revision)
    return latest


def _security_names(run: ScreeningRunRecord | None) -> dict[str, str]:
    if run is None:
        return {}
    result: dict[str, str] = {}
    for row in run.rows:
        ticker = _text(row.get("ticker"))
        name = _text(row.get("name"))
        if ticker is not None and name is not None:
            result[ticker] = name
    return result


def _holding_view(
    holding: HoldingSnapshot,
    *,
    revision: ResearchRevision | None,
    security_name: str | None,
    next_earnings_date: date | None = None,
) -> HoldingView:
    price_value = None if holding.market_price_yen is None else float(holding.market_price_yen)
    price_display = None if holding.market_price_yen is None else str(holding.market_price_yen)
    market_value = holding.market_value_yen
    price_as_of = holding.market_price_observed_at
    pnl = None if market_value is None else market_value - holding.deployed_cost_yen
    fair_value = revision.pmax_raw_yen if revision is not None else None
    fv_gap = (
        round((fair_value - price_value) / price_value * 100, 1)
        if fair_value is not None and price_value is not None and price_value != 0
        else None
    )
    return HoldingView(
        ticker=holding.ticker,
        company_name=revision.company_name if revision is not None else security_name,
        sector=holding.sector,
        quantity=holding.quantity,
        deployed_cost_yen=holding.deployed_cost_yen,
        market_price_yen=price_display,
        market_price_as_of=price_as_of,
        market_value_yen=market_value,
        unrealized_pnl_yen=pnl,
        unrealized_pnl_pct=_percentage(pnl, holding.deployed_cost_yen, digits=2),
        pmax_raw_yen=fair_value,
        pmax_gap_pct=fv_gap,
        latest_thesis_id=revision.thesis_id if revision is not None else None,
        disposition=revision.disposition if revision is not None else None,
        next_earnings_date=(
            next_earnings_date.isoformat() if next_earnings_date is not None else None
        ),
    )


def _upcoming_events(
    *,
    today: date,
    holdings: list[HoldingView],
    reservations: list[ReservationView],
) -> list[UpcomingEventView]:
    """Collapse holding earnings and reservation expiries into one chronological list
    within the next ``_EVENT_WINDOW_DAYS`` days.

    The window is inclusive on both ends: an event dated today (days_until 0) through
    ``today + _EVENT_WINDOW_DAYS`` is surfaced; anything past or beyond is dropped so the
    The app only shows what needs attention now.
    """

    window_end = today + timedelta(days=_EVENT_WINDOW_DAYS)
    events: list[UpcomingEventView] = []
    for holding in holdings:
        if holding.next_earnings_date is None:
            continue
        event_date = date.fromisoformat(holding.next_earnings_date)
        if today <= event_date <= window_end:
            events.append(
                UpcomingEventView(
                    event_date=event_date,
                    kind="earnings",
                    ticker=holding.ticker,
                    label=holding.company_name or holding.ticker,
                    days_until=(event_date - today).days,
                )
            )
    for reservation in reservations:
        event_date = reservation.expires_at.date()
        if today <= event_date <= window_end:
            events.append(
                UpcomingEventView(
                    event_date=event_date,
                    kind="reservation_expiry",
                    ticker=reservation.ticker,
                    label=reservation.ticker,
                    days_until=(event_date - today).days,
                )
            )
    events.sort(key=lambda item: (item.event_date, _EVENT_KIND_ORDER[item.kind], item.ticker or ""))
    return events


def _task_views(
    records: list[TaskRecord],
    *,
    today: date,
) -> tuple[list[TaskView], TaskView | None]:
    open_records = sorted(
        (item for item in records if item.status == "open"),
        key=lambda item: (item.due_date, item.task_id),
    )
    views = [_task_view(item, today=today) for item in open_records]
    return views, views[0] if views else None


def _task_view(record: TaskRecord, *, today: date) -> TaskView:
    return TaskView(
        task_id=record.task_id,
        title=record.title,
        kind=record.kind,
        status=record.status,
        ticker=record.ticker,
        due_date=record.due_date,
        event_label=record.event_label,
        event_date=record.event_date,
        overdue=record.due_date < today,
    )


def _percentage(numerator: int | None, denominator: int | None, *, digits: int) -> float | None:
    if numerator is None or denominator is None:
        return None
    return round(numerator / denominator * 100, digits) if denominator else 0.0


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return float(parsed) if parsed.is_finite() else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


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
        method_hash = _text(method.get("method_hash")) if isinstance(method, Mapping) else None
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
    return _number(expected.get("er_annual")) if isinstance(expected, Mapping) else None


def _review_set_entry_delta(
    row: Mapping[str, object], *, disclosed: bool | None
) -> ReviewSetEntryDeltaView:
    return ReviewSetEntryDeltaView(
        ticker=str(row.get("ticker", "")),
        company_name=_text(row.get("name")),
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
                company_name=_text(current[ticker].get("name")),
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
    revisions = _latest_research_by_ticker(research.revisions())
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
