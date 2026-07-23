"""Compose domain source values into UI-specific read models."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo

from baibai_app.sources.db_sources import (
    DbCandidatesSource,
    DbMacroSource,
    DbMetaSource,
    DbOperationsSource,
)
from baibai_app.sources.protocols import (
    CandidatesSource,
    LedgerSource,
    MacroContextSource,
    MarketPriceSource,
    ResearchSource,
    TaskSource,
)
from baibai_app.sources.types import CandidatesRun, ResearchRevision, TaskRecord, ThesisDetail
from baibai_engine.read_api import (
    HoldingSnapshot,
    MacroGranularity,
    PortfolioLedgerError,
    PortfolioSnapshot,
    macro_series_names,
)

from .models import (
    CandidateRowView,
    DashboardView,
    HoldingReviewView,
    HoldingView,
    MachineSelectionView,
    MacroContextRevisionView,
    MacroContextSectionView,
    MacroContextView,
    MacroFactSummaryView,
    MacroGroupView,
    MacroInvestmentConnectionView,
    MacroMaterialDeltaView,
    MacroMonitoringPointView,
    MacroPointView,
    MacroScenarioView,
    MacroSectionJudgmentView,
    MacroSeriesReferenceView,
    MacroSeriesView,
    MacroSizingCautionView,
    MacroView,
    MetaBatch,
    MetaView,
    OperationSessionView,
    OperationsView,
    PortfolioOutcomeView,
    PortfolioState,
    ProposalView,
    ResearchRevisionView,
    ReservationView,
    ScenarioView,
    ScreeningRunView,
    ScreeningView,
    SecurityDetailView,
    ShortlistEntryView,
    ShortlistView,
    TaskView,
    ThesisDetailView,
    UpcomingEventView,
    WarningView,
)

_EVENT_WINDOW_DAYS = 14
# Order same-day events so the read most likely to gate an imminent action leads:
# an earnings print, then a reservation lapse, then the macro context expiry.
_EVENT_KIND_ORDER = {"earnings": 0, "reservation_expiry": 1, "macro_valid_until": 2}

type MacroPeriod = Literal["1y", "5y", "10y", "max"]

_JST = ZoneInfo("Asia/Tokyo")
# Daily bars carry a trade date only; stamp the display timestamp at the TSE close so
# a market-derived price reads as an end-of-session observation.
_MARKET_CLOSE_TIME = time(15, 30)
_NUMERIC_FIELDS = (
    "market_cap_oku",
    "avg_turnover_oku",
    "per_trailing",
    "per_forward",
    "pbr",
    "ev_ebitda",
    "p_s",
    "pcfr",
    "price_change_20d",
    "gap_from_52w_low",
    "sector_relative_strength_percentile",
)
_METRIC_FIELDS = (
    "dividend_yield",
    "er_annual",
    "er_reversion_annual",
    "er_carry_annual",
    "net_cash_to_market_cap",
    "fcf_yield",
    "ocf_yield",
    "equity_ratio",
    "sales_yoy",
    "operating_profit_yoy",
)
_STALE_RUN_AGE = timedelta(days=7)


def build_meta(source: DbMetaSource, *, batch: MetaBatch | None = None) -> MetaView:
    """Report per-store as-of freshness so a consumer can judge view staleness."""

    return MetaView(
        generated_at=datetime.now(_JST),
        screening_asof=source.screening_asof(),
        macro_asof=source.macro_asof(),
        app_db_updated_at=source.app_db_updated_at(),
        batch=batch,
    )


def build_operations_view(source: DbOperationsSource) -> OperationsView:
    return OperationsView(
        operations=[OperationSessionView.model_validate(item) for item in source.operations()],
        proposals=[ProposalView.model_validate(item) for item in source.proposals()],
        outcomes=[PortfolioOutcomeView.model_validate(item) for item in source.outcomes()],
    )


def build_dashboard(
    ledger: LedgerSource,
    research: ResearchSource,
    tasks: TaskSource,
    candidates: CandidatesSource,
    market: MarketPriceSource,
    *,
    macro: MacroContextSource | None = None,
) -> DashboardView:
    """Build the Baibai App first view without performing storage I/O directly."""

    now = datetime.now(_JST)
    today = now.date()
    revisions = research.revisions()
    latest_research = _latest_research_by_ticker(revisions)
    latest_run = candidates.latest_run()
    candidate_names = _candidate_names(latest_run)
    open_tasks, next_task, next_event = _task_views(tasks.list_tasks(), today=today)
    tasks_exist = tasks.exists()
    research_load_errors = research.load_errors()
    macro_valid_until = _macro_valid_until(macro, as_of=today)

    if not ledger.exists():
        return _empty_dashboard(
            generated_at=now,
            ledger_exists=False,
            ledger_error=None,
            open_tasks=open_tasks,
            next_task=next_task,
            next_event=next_event,
            tasks_exist=tasks_exist,
            research_load_errors=research_load_errors,
            upcoming_events=_upcoming_events(
                today=today, holdings=[], reservations=[], macro_valid_until=macro_valid_until
            ),
        )
    try:
        snapshot = ledger.snapshot()
    except PortfolioLedgerError as error:
        return _empty_dashboard(
            generated_at=now,
            ledger_exists=True,
            ledger_error=str(error),
            open_tasks=open_tasks,
            next_task=next_task,
            next_event=next_event,
            tasks_exist=tasks_exist,
            research_load_errors=research_load_errors,
            upcoming_events=_upcoming_events(
                today=today, holdings=[], reservations=[], macro_valid_until=macro_valid_until
            ),
        )

    holding_tickers = [holding.ticker for holding in snapshot.holdings]
    market_closes = market.latest_closes(holding_tickers)
    earnings_dates = market.next_earnings_dates(holding_tickers, asof=today)
    holdings = [
        _holding_view(
            holding,
            revision=latest_research.get(holding.ticker),
            candidate_name=candidate_names.get(holding.ticker),
            market_close=market_closes.get(holding.ticker),
            next_earnings_date=earnings_dates.get(holding.ticker),
        )
        for holding in snapshot.holdings
    ]
    holdings.sort(key=lambda item: item.market_value_yen, reverse=True)
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
    # Re-total from the displayed holding values so a fresher market close flows into the
    # header aggregates. Cash legs stay canonical; total = cash + reserved + market value,
    # the same identity reconcile_portfolio uses, so the ledger-only case is unchanged.
    holdings_market_value = sum(item.market_value_yen for item in holdings)
    available_cash = snapshot.available_cash_yen
    reserved_cash = snapshot.reserved_cash_yen
    total = available_cash + reserved_cash + holdings_market_value
    return DashboardView(
        generated_at=now,
        ledger_exists=True,
        ledger_error=None,
        ledger_as_of=snapshot.as_of,
        ledger_stale=snapshot.as_of.date() <= today - timedelta(days=7),
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
        upcoming_events=_upcoming_events(
            today=today,
            holdings=holdings,
            reservations=reservations,
            macro_valid_until=macro_valid_until,
        ),
        open_tasks=open_tasks,
        next_task=next_task,
        next_event=next_event,
        tasks_exist=tasks_exist,
        research_load_errors=research_load_errors,
    )


def build_screening(
    candidates: CandidatesSource,
    ledger: LedgerSource,
    research: ResearchSource,
) -> ScreeningView:
    """Build the latest candidates table with portfolio/research annotations."""

    run = candidates.latest_run()
    selections: list[MachineSelectionView] = []
    shortlists: list[ShortlistView] = []
    if isinstance(candidates, DbCandidatesSource):
        # Operative run: judgment publications (selection / shortlist) bind to a
        # specific run revision. A newer revision of the same as-of (e.g. a determinism
        # re-run) must not present the Baibai App with an empty machine-selection view, so
        # when the latest run has no selection we fall back to the newest selection's run
        # and keep the candidates table, selections, and shortlist join coherent.
        all_selections = candidates.selections()
        run_selections = (
            [item for item in all_selections if str(item["run_revision_id"]) == run.source_path]
            if run is not None
            else []
        )
        if run is not None and not run_selections and all_selections:
            newest = max(all_selections, key=lambda item: str(item["created_at"]))
            fallback_run = candidates.run(str(newest["run_revision_id"]))
            if fallback_run is not None:
                run = fallback_run
                run_selections = [
                    item
                    for item in all_selections
                    if str(item["run_revision_id"]) == run.source_path
                ]
        selections = [_machine_selection_view(item) for item in run_selections]
        shortlists = [_shortlist_view(item) for item in candidates.shortlists()]
    if run is None:
        return ScreeningView(
            run=None,
            rows=[],
            selections=selections,
            shortlists=shortlists,
        )
    held, reserved = _held_and_reserved_tickers(ledger)
    researched = {item.ticker for item in research.revisions()}
    today = datetime.now(_JST).date()
    return ScreeningView(
        run=_screening_run_view(run, today=today),
        rows=[
            _candidate_row_view(row, held=held, reserved=reserved, researched=researched)
            for row in run.rows
        ],
        selections=selections,
        shortlists=shortlists,
    )


def _machine_selection_view(raw: Mapping[str, object]) -> MachineSelectionView:
    payload = raw.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("machine selection payload must be an object")
    return MachineSelectionView(
        selection_id=str(raw["selection_id"]),
        run_revision_id=str(raw["run_revision_id"]),
        profile=str(raw["profile"]),
        macro_context_id=_text(raw.get("macro_context_id")),
        created_at=datetime.fromisoformat(str(raw["created_at"])),
        recommendations=[dict(item) for item in _mapping_items(payload.get("recommendations"))],
        longlist=[dict(item) for item in _mapping_items_optional(payload.get("longlist"))],
    )


def _shortlist_view(raw: Mapping[str, object]) -> ShortlistView:
    return ShortlistView(
        shortlist_id=str(raw["shortlist_id"]),
        selection_id=str(raw["selection_id"]),
        run_revision_id=str(raw["run_revision_id"]),
        as_of=date.fromisoformat(str(raw["as_of"])),
        published_at=datetime.fromisoformat(str(raw["published_at"])),
        entries=[
            ShortlistEntryView.model_validate(item) for item in _mapping_items(raw.get("entries"))
        ],
    )


def _mapping_items_optional(value: object) -> list[Mapping[str, object]]:
    return [] if value is None else _mapping_items(value)


def build_security_detail(
    ticker: str,
    ledger: LedgerSource,
    research: ResearchSource,
    candidates: CandidatesSource,
    market: MarketPriceSource,
) -> SecurityDetailView | None:
    """Build one security page, returning None only when no source knows the ticker."""

    revisions = [item for item in research.revisions() if item.ticker == ticker]
    latest_revision = revisions[0] if revisions else None
    run = candidates.latest_run()
    raw_candidate = (
        next(
            (row for row in run.rows if str(row.get("ticker", "")) == ticker),
            None,
        )
        if run is not None
        else None
    )
    snapshot = _safe_snapshot(ledger)
    holding_snapshot = (
        next((item for item in snapshot.holdings if item.ticker == ticker), None)
        if snapshot is not None
        else None
    )
    if holding_snapshot is None and latest_revision is None and raw_candidate is None:
        return None

    candidate_name = _text(raw_candidate.get("name")) if raw_candidate is not None else None
    candidate_sector = _text(raw_candidate.get("sector_33")) if raw_candidate is not None else None
    holding = (
        _holding_view(
            holding_snapshot,
            revision=latest_revision,
            candidate_name=candidate_name,
            market_close=market.latest_closes([ticker]).get(ticker),
            next_earnings_date=market.next_earnings_dates(
                [ticker], asof=datetime.now(_JST).date()
            ).get(ticker),
        )
        if holding_snapshot is not None
        else None
    )
    latest_thesis = (
        _thesis_detail_view(research.thesis_detail(latest_revision.thesis_id))
        if latest_revision is not None
        else None
    )
    reserved_here = (
        {ticker}
        if snapshot is not None
        and any(item.ticker == ticker for item in snapshot.active_reservations)
        else set()
    )
    candidate_row = (
        _candidate_row_view(
            raw_candidate,
            held={ticker} if holding_snapshot is not None else set(),
            reserved=reserved_here,
            researched={ticker} if revisions else set(),
        )
        if raw_candidate is not None
        else None
    )
    return SecurityDetailView(
        ticker=ticker,
        company_name=(
            latest_revision.company_name if latest_revision is not None else candidate_name
        ),
        sector=(
            latest_revision.sector
            if latest_revision is not None
            else candidate_sector
            or (holding_snapshot.sector if holding_snapshot is not None else None)
        ),
        holding=holding,
        revisions=[_research_revision_view(item) for item in revisions],
        latest_thesis=latest_thesis,
        holding_reviews=[
            HoldingReviewView(
                holding_review_id=item.holding_review_id,
                as_of=item.as_of,
                thesis_id=item.thesis_id,
                candidate_thesis_id=item.candidate_thesis_id,
                action=item.action,
                note=item.note,
            )
            for item in research.holding_reviews(ticker=ticker)
        ],
        candidate_row=candidate_row,
        candidate_run=(
            _screening_run_view(run, today=datetime.now(_JST).date()) if run is not None else None
        ),
    )


def build_macro(
    source: DbMacroSource,
    *,
    as_of: date,
    period: MacroPeriod = "1y",
    granularity: MacroGranularity = "daily",
) -> MacroView:
    raw_context = source.context(as_of=as_of)
    context = None
    if raw_context is not None:
        valid_until = date.fromisoformat(str(raw_context["valid_until"]))
        series_names = macro_series_names()
        context = MacroContextView(
            context_id=str(raw_context["context_id"]),
            as_of=date.fromisoformat(str(raw_context["as_of"])),
            valid_until=valid_until,
            published_at=datetime.fromisoformat(str(raw_context["published_at"])),
            summary=str(raw_context["summary"]),
            stale=valid_until < as_of,
            sections=[
                _macro_context_section_view(item, series_names=series_names)
                for item in _optional_mapping_items(raw_context.get("sections"))
            ],
        )
    groups: list[MacroGroupView] = []
    period_start = _macro_period_start(as_of, period=period)
    for group in source.groups:
        series_views: list[MacroSeriesView] = []
        for configured in group.series:
            raw_series = source.series(
                configured.series_id,
                start=period_start,
                end=as_of,
                granularity=granularity,
            )
            if raw_series is None:
                raise ValueError(f"configured macro series is unavailable: {configured.series_id}")
            series_views.append(
                MacroSeriesView(
                    series_id=configured.series_id,
                    label=configured.label,
                    name=str(raw_series["name"]),
                    unit=str(raw_series["unit"]),
                    tradingview_symbol=(
                        str(raw_series["tradingview_symbol"])
                        if raw_series.get("tradingview_symbol") is not None
                        else None
                    ),
                    points=[
                        MacroPointView.model_validate(item)
                        for item in _mapping_items(raw_series["points"])
                    ],
                )
            )
        groups.append(MacroGroupView(title=group.title, series=series_views))
    history = [
        MacroContextRevisionView(
            context_id=str(item["context_id"]),
            as_of=date.fromisoformat(str(item["as_of"])),
            valid_until=date.fromisoformat(str(item["valid_until"])),
            published_at=datetime.fromisoformat(str(item["published_at"])),
            summary=str(item["summary"]),
        )
        for item in source.contexts()
    ]
    return MacroView(
        as_of=as_of,
        period=period,
        granularity=granularity,
        context=context,
        context_history=history,
        groups=groups,
    )


def _macro_period_start(as_of: date, *, period: MacroPeriod) -> date | None:
    if period == "max":
        return None
    years = {"1y": 1, "5y": 5, "10y": 10}[period]
    try:
        return as_of.replace(year=as_of.year - years)
    except ValueError:
        return as_of.replace(year=as_of.year - years, day=28)


def _mapping_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError("expected an array of objects")
    return [item for item in value if isinstance(item, Mapping)]


def _optional_mapping_items(value: object) -> list[Mapping[str, object]]:
    if value is None:
        return []
    return _mapping_items(value)


def _macro_context_section_view(
    raw: Mapping[str, object], *, series_names: Mapping[str, str]
) -> MacroContextSectionView:
    series_ids = _string_items(raw.get("series_ids"))
    return MacroContextSectionView(
        section_id=str(raw["section_id"]),
        series=[
            MacroSeriesReferenceView(
                series_id=series_id, name=series_names.get(series_id, series_id)
            )
            for series_id in series_ids
        ],
        fact_summary=[
            MacroFactSummaryView.model_validate(item)
            for item in _mapping_items(raw.get("fact_summary"))
        ],
        judgment=MacroSectionJudgmentView.model_validate(raw["judgment"]),
        investment_connection=MacroInvestmentConnectionView.model_validate(
            raw["investment_connection"]
        ),
        change_since_previous=(
            str(raw["change_since_previous"])
            if raw.get("change_since_previous") is not None
            else None
        ),
        material_deltas=[
            MacroMaterialDeltaView.model_validate(item)
            for item in _mapping_items(raw.get("material_deltas"))
        ],
        sizing_cautions=[
            MacroSizingCautionView.model_validate(item)
            for item in _mapping_items(raw.get("sizing_cautions"))
        ],
        scenarios=[
            MacroScenarioView.model_validate(item) for item in _mapping_items(raw.get("scenarios"))
        ],
        monitoring_points=[
            MacroMonitoringPointView.model_validate(item)
            for item in _mapping_items(raw.get("monitoring_points"))
        ],
    )


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("expected an array of strings")
    return value


def _empty_dashboard(
    *,
    generated_at: datetime,
    ledger_exists: bool,
    ledger_error: str | None,
    open_tasks: list[TaskView],
    next_task: TaskView | None,
    next_event: TaskView | None,
    tasks_exist: bool,
    research_load_errors: list[str],
    upcoming_events: list[UpcomingEventView],
) -> DashboardView:
    return DashboardView(
        generated_at=generated_at,
        ledger_exists=ledger_exists,
        ledger_error=ledger_error,
        ledger_as_of=None,
        ledger_stale=False,
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
        upcoming_events=upcoming_events,
        open_tasks=open_tasks,
        next_task=next_task,
        next_event=next_event,
        tasks_exist=tasks_exist,
        research_load_errors=research_load_errors,
    )


def _latest_research_by_ticker(
    revisions: list[ResearchRevision],
) -> dict[str, ResearchRevision]:
    latest: dict[str, ResearchRevision] = {}
    for revision in revisions:
        latest.setdefault(revision.ticker, revision)
    return latest


def _candidate_names(run: CandidatesRun | None) -> dict[str, str]:
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
    candidate_name: str | None,
    market_close: tuple[float, date] | None = None,
    next_earnings_date: date | None = None,
) -> HoldingView:
    # The canonical ledger price is a human-confirmed observation; when the read-only
    # market store carries a strictly newer close, value the holding on that close so the
    # Baibai App does not lag stale ledger prices. Anything not newer keeps the ledger value.
    price_value = float(holding.market_price_yen)
    price_display = str(holding.market_price_yen)
    market_value = holding.market_value_yen
    price_as_of = holding.market_price_observed_at
    if market_close is not None:
        close_price, close_date = market_close
        if close_date > holding.market_price_observed_at.date():
            price_value = close_price
            price_display = _format_market_price(close_price)
            market_value = round(close_price * holding.quantity)
            price_as_of = datetime.combine(close_date, _MARKET_CLOSE_TIME, tzinfo=_JST)
    pnl = market_value - holding.deployed_cost_yen
    fair_value = revision.current_fair_value_yen if revision is not None else None
    fv_gap = (
        round((fair_value - price_value) / price_value * 100, 1)
        if fair_value is not None and price_value != 0
        else None
    )
    return HoldingView(
        ticker=holding.ticker,
        company_name=revision.company_name if revision is not None else candidate_name,
        sector=holding.sector,
        quantity=holding.quantity,
        deployed_cost_yen=holding.deployed_cost_yen,
        market_price_yen=price_display,
        market_price_as_of=price_as_of,
        market_value_yen=market_value,
        unrealized_pnl_yen=pnl,
        unrealized_pnl_pct=_percentage(pnl, holding.deployed_cost_yen, digits=2),
        fair_value_yen=fair_value,
        fv_gap_pct=fv_gap,
        latest_thesis_id=revision.thesis_id if revision is not None else None,
        recommendation=revision.recommendation if revision is not None else None,
        next_earnings_date=(
            next_earnings_date.isoformat() if next_earnings_date is not None else None
        ),
    )


def _macro_valid_until(macro: MacroContextSource | None, *, as_of: date) -> date | None:
    if macro is None:
        return None
    context = macro.context(as_of=as_of)
    if context is None:
        return None
    raw = context.get("valid_until")
    return date.fromisoformat(str(raw)) if raw is not None else None


def _upcoming_events(
    *,
    today: date,
    holdings: list[HoldingView],
    reservations: list[ReservationView],
    macro_valid_until: date | None,
) -> list[UpcomingEventView]:
    """Collapse holding earnings, reservation expiries, and the macro context expiry
    into one chronological list within the next ``_EVENT_WINDOW_DAYS`` days.

    The window is inclusive on both ends: an event dated today (days_until 0) through
    ``today + _EVENT_WINDOW_DAYS`` is surfaced; anything past or beyond is dropped so the
    Baibai App only shows what needs attention now.
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
    if macro_valid_until is not None and today <= macro_valid_until <= window_end:
        events.append(
            UpcomingEventView(
                event_date=macro_valid_until,
                kind="macro_valid_until",
                ticker=None,
                label="マクロcontext有効期限",
                days_until=(macro_valid_until - today).days,
            )
        )
    events.sort(key=lambda item: (item.event_date, _EVENT_KIND_ORDER[item.kind], item.ticker or ""))
    return events


def _format_market_price(value: float) -> str:
    """Render a market close like the ledger's decimal price, dropping a bare .0."""

    return str(int(value)) if value.is_integer() else str(value)


def _task_views(
    records: list[TaskRecord],
    *,
    today: date,
) -> tuple[list[TaskView], TaskView | None, TaskView | None]:
    open_records = sorted(
        (item for item in records if item.status == "open"),
        key=lambda item: (item.due_date, item.task_id),
    )
    views = [_task_view(item, today=today) for item in open_records]
    next_task = views[0] if views else None
    event_records = sorted(
        (item for item in open_records if item.event_date is not None),
        key=lambda item: (item.event_date, item.task_id),
    )
    next_event = _task_view(event_records[0], today=today) if event_records else next_task
    return views, next_task, next_event


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


def _safe_snapshot(ledger: LedgerSource) -> PortfolioSnapshot | None:
    if not ledger.exists():
        return None
    try:
        return ledger.snapshot()
    except PortfolioLedgerError:
        return None


def _held_and_reserved_tickers(ledger: LedgerSource) -> tuple[set[str], set[str]]:
    snapshot = _safe_snapshot(ledger)
    if snapshot is None:
        return set(), set()
    held = {item.ticker for item in snapshot.holdings}
    reserved = {item.ticker for item in snapshot.active_reservations}
    return held, reserved


def _portfolio_state(ticker: str, *, held: set[str], reserved: set[str]) -> PortfolioState:
    is_held = ticker in held
    is_reserved = ticker in reserved
    if is_held and is_reserved:
        return "held_and_reserved"
    if is_held:
        return "held"
    if is_reserved:
        return "reserved"
    return "unheld"


def _data_quality_flags(row: Mapping[str, object], metrics: Mapping[str, object]) -> list[str]:
    """Surface stale/incomplete data so no metric is trusted silently in the table."""

    flags: list[str] = []
    ttm_quality = row.get("ttm_quality")
    if isinstance(ttm_quality, Mapping) and any(value != "exact" for value in ttm_quality.values()):
        flags.append("TTM非exact")
    if metrics.get("edinet_failure_reasons"):
        flags.append("EDINET失敗")
    lag = metrics.get("bs_carry_forward_lag_days")
    if isinstance(lag, (int, float)) and not isinstance(lag, bool) and lag > 0:
        flags.append("BS前期繰越")
    if row.get("freshness_warnings"):
        flags.append("鮮度warning")
    if row.get("split_adjustment_flag") is True:
        flags.append("分割補正")
    if metrics.get("forecast_special_gain_flag") is True:
        flags.append("一時益予想")
    return flags


def _screening_run_view(run: CandidatesRun, *, today: date) -> ScreeningRunView:
    return ScreeningRunView(
        run_id=run.run_id,
        run_date=run.run_date,
        asof_date=run.asof_date,
        run_at=run.run_at,
        universe_size=run.universe_size,
        candidate_count=len(run.rows),
        source_path=run.source_path,
        application_git_commit=run.application_git_commit,
        stale=run.asof_date <= today - _STALE_RUN_AGE,
    )


def _candidate_row_view(
    row: Mapping[str, object],
    *,
    held: set[str],
    reserved: set[str],
    researched: set[str],
) -> CandidateRowView:
    ticker = str(row.get("ticker", ""))
    metrics_raw = row.get("metrics")
    metrics = metrics_raw if isinstance(metrics_raw, Mapping) else {}
    values = {name: _number(row.get(name)) for name in _NUMERIC_FIELDS}
    values.update({name: _number(metrics.get(name)) for name in _METRIC_FIELDS})
    flags = _data_quality_flags(row, metrics)
    return CandidateRowView(
        ticker=ticker,
        name=_text(row.get("name")),
        sector_33=_text(row.get("sector_33")),
        next_earnings_date=_text(row.get("next_earnings_date")),
        data_quality_flags=flags,
        bargain_score=_bargain_score(
            values["er_reversion_annual"], values["er_carry_annual"], len(flags)
        ),
        portfolio_state=_portfolio_state(ticker, held=held, reserved=reserved),
        has_research=ticker in researched,
        **values,
    )


def _bargain_score(reversion: float | None, carry: float | None, flag_count: int) -> float | None:
    # Display-only ordering that centers the evidence of cheapness. Reversion (the pull
    # back to fair value) carries full weight; carry (dividend / buyback yield) is a
    # holding-period return, so it enters at half weight and is clipped at 15%/y — a carry
    # beyond that is a special dividend or a data anomaly, not a sustainable yield, and must
    # not dominate the ordering. Each data-quality flag is a small confidence discount.
    # This is a Baibai App view score, not a canonical ranking.
    if reversion is None and carry is None:
        return None
    clipped_carry = min(carry or 0.0, 0.15)
    return round((reversion or 0.0) + 0.5 * clipped_carry - 0.005 * flag_count, 6)


def _research_revision_view(revision: ResearchRevision) -> ResearchRevisionView:
    return ResearchRevisionView(
        as_of=revision.as_of,
        thesis_id=revision.thesis_id,
        recommendation=revision.recommendation,
        confidence=revision.confidence,
        current_fair_value_yen=revision.current_fair_value_yen,
        model_version=revision.model_version,
        review_id=revision.review_id,
    )


def _thesis_detail_view(detail: ThesisDetail) -> ThesisDetailView:
    return ThesisDetailView(
        revision=_research_revision_view(detail.revision),
        entry_price_basis_yen=detail.entry_price_basis_yen,
        required_5y_base_cagr_pct=detail.required_5y_base_cagr_pct,
        permanent_loss_risk_count=detail.permanent_loss_risk_count,
        scenarios=[
            ScenarioView(name=item.name, horizon_years=item.horizon_years)
            for item in detail.scenarios
        ],
        permanent_loss_conclusion=detail.permanent_loss_conclusion,
        strongest_countercase=detail.strongest_countercase,
        sizing_action=detail.sizing_action,
    )


def _percentage(numerator: int, denominator: int, *, digits: int) -> float:
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
