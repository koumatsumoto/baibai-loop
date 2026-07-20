"""Compose domain source values into UI-specific read models."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo

from baibai_app.sources.db_sources import DbCandidatesSource, DbMacroSource, DbProgramSource
from baibai_app.sources.protocols import (
    CandidatesSource,
    LedgerSource,
    ResearchSource,
    TaskSource,
)
from baibai_app.sources.types import CandidatesRun, PacketDetail, ResearchRevision, TaskRecord
from baibai_engine.read_api import (
    HoldingSnapshot,
    MacroGranularity,
    PortfolioLedgerError,
    PortfolioSnapshot,
)

from .models import (
    CandidateRowView,
    DashboardView,
    HoldingReviewView,
    HoldingView,
    MachineSelectionView,
    MacroContextRevisionView,
    MacroContextView,
    MacroGroupView,
    MacroMaterialDeltaView,
    MacroPointView,
    MacroSeriesView,
    MacroSizingCautionView,
    MacroView,
    OperationSessionView,
    PacketDetailView,
    PortfolioOutcomeView,
    ProgramStateView,
    ProposalView,
    ResearchRevisionView,
    ReservationView,
    ReviewedShortlistEntryView,
    ReviewedShortlistView,
    ScenarioView,
    ScreeningRunView,
    ScreeningView,
    SecurityDetailView,
    TaskView,
    WarningView,
)

type MacroPeriod = Literal["1y", "5y", "10y", "max"]

_JST = ZoneInfo("Asia/Tokyo")
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
)


def build_program_state(source: DbProgramSource) -> ProgramStateView:
    return ProgramStateView(
        operations=[OperationSessionView.model_validate(item) for item in source.operations()],
        proposals=[ProposalView.model_validate(item) for item in source.proposals()],
        outcomes=[PortfolioOutcomeView.model_validate(item) for item in source.outcomes()],
    )


def build_dashboard(
    ledger: LedgerSource,
    research: ResearchSource,
    tasks: TaskSource,
    candidates: CandidatesSource,
) -> DashboardView:
    """Build the cockpit first view without performing storage I/O directly."""

    now = datetime.now(_JST)
    today = now.date()
    revisions = research.revisions()
    latest_research = _latest_research_by_ticker(revisions)
    latest_run = candidates.latest_run()
    candidate_names = _candidate_names(latest_run)
    open_tasks, next_task, next_event = _task_views(tasks.list_tasks(), today=today)
    tasks_exist = tasks.exists()
    research_load_errors = research.load_errors()

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
        )

    holdings = [
        _holding_view(
            holding,
            revision=latest_research.get(holding.ticker),
            candidate_name=candidate_names.get(holding.ticker),
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
    total = snapshot.total_capital_yen
    return DashboardView(
        generated_at=now,
        ledger_exists=True,
        ledger_error=None,
        ledger_as_of=snapshot.as_of,
        ledger_stale=snapshot.as_of.date() <= today - timedelta(days=7),
        total_capital_yen=total,
        available_cash_yen=snapshot.available_cash_yen,
        reserved_cash_yen=snapshot.reserved_cash_yen,
        holdings_market_value_yen=snapshot.holdings_market_value_yen,
        deployed_cost_yen=snapshot.deployed_cost_yen,
        cash_pct=_percentage(snapshot.available_cash_yen, total, digits=1),
        reserved_pct=_percentage(snapshot.reserved_cash_yen, total, digits=1),
        deployed_pct=_percentage(snapshot.holdings_market_value_yen, total, digits=1),
        holdings=holdings,
        reservations=reservations,
        warnings=warnings,
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
    shortlists: list[ReviewedShortlistView] = []
    if isinstance(candidates, DbCandidatesSource):
        selected_id = None if run is None else run.source_path
        selections = [
            _machine_selection_view(item)
            for item in candidates.selections(run_revision_id=selected_id)
        ]
        shortlists = [_reviewed_shortlist_view(item) for item in candidates.reviewed_shortlists()]
    if run is None:
        return ScreeningView(
            run=None,
            rows=[],
            selections=selections,
            reviewed_shortlists=shortlists,
        )
    held = _held_tickers(ledger)
    researched = {item.ticker for item in research.revisions()}
    return ScreeningView(
        run=_screening_run_view(run),
        rows=[_candidate_row_view(row, held=held, researched=researched) for row in run.rows],
        selections=selections,
        reviewed_shortlists=shortlists,
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
        audit_pool=[dict(item) for item in _mapping_items_optional(payload.get("audit_pool"))],
    )


def _reviewed_shortlist_view(raw: Mapping[str, object]) -> ReviewedShortlistView:
    return ReviewedShortlistView(
        shortlist_id=str(raw["shortlist_id"]),
        selection_id=str(raw["selection_id"]),
        run_revision_id=str(raw["run_revision_id"]),
        published_at=datetime.fromisoformat(str(raw["published_at"])),
        entries=[
            ReviewedShortlistEntryView.model_validate(item)
            for item in _mapping_items(raw.get("entries"))
        ],
    )


def _mapping_items_optional(value: object) -> list[Mapping[str, object]]:
    return [] if value is None else _mapping_items(value)


def build_security_detail(
    ticker: str,
    ledger: LedgerSource,
    research: ResearchSource,
    candidates: CandidatesSource,
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
        )
        if holding_snapshot is not None
        else None
    )
    latest_packet = (
        _packet_detail_view(research.packet_detail(latest_revision.packet_id))
        if latest_revision is not None
        else None
    )
    candidate_row = (
        _candidate_row_view(
            raw_candidate,
            held={ticker} if holding_snapshot is not None else set(),
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
        latest_packet=latest_packet,
        holding_reviews=[
            HoldingReviewView(
                holding_review_id=item.holding_review_id,
                as_of=item.as_of,
                packet_id=item.packet_id,
                candidate_packet_id=item.candidate_packet_id,
                action=item.action,
                note=item.note,
            )
            for item in research.holding_reviews(ticker=ticker)
        ],
        candidate_row=candidate_row,
        candidate_run=_screening_run_view(run) if run is not None else None,
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
        context = MacroContextView(
            context_id=str(raw_context["context_id"]),
            as_of=date.fromisoformat(str(raw_context["as_of"])),
            valid_until=valid_until,
            published_at=datetime.fromisoformat(str(raw_context["published_at"])),
            summary=str(raw_context["summary"]),
            stale=valid_until < as_of,
            material_deltas=[
                MacroMaterialDeltaView.model_validate(item)
                for item in _mapping_items(raw_context.get("material_deltas"))
            ],
            sizing_cautions=[
                MacroSizingCautionView.model_validate(item)
                for item in _mapping_items(raw_context.get("sizing_cautions"))
            ],
            research_questions=_string_items(raw_context.get("research_questions")),
            refresh_triggers=_string_items(raw_context.get("refresh_triggers")),
            changes_since_previous=_string_items(raw_context.get("changes_since_previous")),
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
) -> HoldingView:
    pnl = holding.market_value_yen - holding.deployed_cost_yen
    fair_value = revision.current_fair_value_yen if revision is not None else None
    market_price = holding.market_price_yen
    fv_gap = (
        round((fair_value - float(market_price)) / float(market_price) * 100, 1)
        if fair_value is not None and market_price != 0
        else None
    )
    return HoldingView(
        ticker=holding.ticker,
        company_name=revision.company_name if revision is not None else candidate_name,
        sector=holding.sector,
        quantity=holding.quantity,
        deployed_cost_yen=holding.deployed_cost_yen,
        market_price_yen=str(market_price),
        market_price_as_of=holding.market_price_observed_at,
        market_value_yen=holding.market_value_yen,
        unrealized_pnl_yen=pnl,
        unrealized_pnl_pct=_percentage(pnl, holding.deployed_cost_yen, digits=2),
        fair_value_yen=fair_value,
        fv_gap_pct=fv_gap,
        latest_packet_id=revision.packet_id if revision is not None else None,
        recommendation=revision.recommendation if revision is not None else None,
    )


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


def _held_tickers(ledger: LedgerSource) -> set[str]:
    snapshot = _safe_snapshot(ledger)
    return {item.ticker for item in snapshot.holdings} if snapshot is not None else set()


def _screening_run_view(run: CandidatesRun) -> ScreeningRunView:
    return ScreeningRunView(
        run_id=run.run_id,
        run_date=run.run_date,
        asof_date=run.asof_date,
        universe_size=run.universe_size,
        candidate_count=len(run.rows),
        source_path=run.source_path,
        application_git_commit=run.application_git_commit,
    )


def _candidate_row_view(
    row: Mapping[str, object],
    *,
    held: set[str],
    researched: set[str],
) -> CandidateRowView:
    ticker = str(row.get("ticker", ""))
    metrics_raw = row.get("metrics")
    metrics = metrics_raw if isinstance(metrics_raw, Mapping) else {}
    values = {name: _number(row.get(name)) for name in _NUMERIC_FIELDS}
    values.update({name: _number(metrics.get(name)) for name in _METRIC_FIELDS})
    return CandidateRowView(
        ticker=ticker,
        name=_text(row.get("name")),
        sector_33=_text(row.get("sector_33")),
        next_earnings_date=_text(row.get("next_earnings_date")),
        held=ticker in held,
        has_research=ticker in researched,
        **values,
    )


def _research_revision_view(revision: ResearchRevision) -> ResearchRevisionView:
    return ResearchRevisionView(
        as_of=revision.as_of,
        packet_id=revision.packet_id,
        recommendation=revision.recommendation,
        confidence=revision.confidence,
        current_fair_value_yen=revision.current_fair_value_yen,
        model_version=revision.model_version,
        review_id=revision.review_id,
    )


def _packet_detail_view(detail: PacketDetail) -> PacketDetailView:
    return PacketDetailView(
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
