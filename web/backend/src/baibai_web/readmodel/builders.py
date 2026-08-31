"""Compose domain source values into UI-specific read models."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from baibai_engine.read_api import (
    MACRO_CONTEXT_STALE_DAYS,
    HoldingSnapshot,
    PortfolioLedgerError,
    PortfolioSnapshot,
    macro_registered_series,
    macro_series_names,
)
from baibai_web.sources.db_sources import (
    DbCandidatesSource,
    DbMacroSource,
    DbMetaSource,
    DbOperationsSource,
)
from baibai_web.sources.protocols import (
    CandidatesSource,
    LedgerSource,
    MarketPriceSource,
    ResearchSource,
    TaskSource,
)
from baibai_web.sources.types import (
    CandidatesRun,
    ErLevelCalibrationContext,
    MacroSeriesConfig,
    ResearchRevision,
    TaskRecord,
    ThesisDetail,
)

from .models import (
    AllocationAlternativeView,
    AssessmentReviewView,
    CandidateEntryDeltaView,
    CandidateMoveDeltaView,
    CapitalAllocationAssessmentSummaryView,
    CapitalAllocationAssessmentView,
    DailyDeltaView,
    DashboardView,
    DeltaPool,
    DeltaUnavailable,
    ErLevelCalibrationBandView,
    ErLevelCalibrationBasisView,
    ErLevelCalibrationContextView,
    ErLevelCalibrationHorizonView,
    ErLevelCalibrationStatsView,
    FvConvergenceView,
    HoldingDeltaView,
    HoldingView,
    MacroComparisonView,
    MacroConnectionSectionView,
    MacroContextExcerptView,
    MacroContextRevisionView,
    MacroContextView,
    MacroCoreSectionView,
    MacroDominantForceView,
    MacroEconomicConnectionView,
    MacroEstimateCaveatView,
    MacroFactSummaryView,
    MacroForceInteractionView,
    MacroGroupView,
    MacroMachineUpdateView,
    MacroMaterialDeltaView,
    MacroMonitoringPointView,
    MacroPointView,
    MacroReadingView,
    MacroResearchPriorityHintView,
    MacroRiskEnvironmentView,
    MacroScenarioView,
    MacroSectionJudgmentView,
    MacroSectorTiltView,
    MacroSeriesChangeView,
    MacroSeriesFetchHealthView,
    MacroSeriesReferenceView,
    MacroSeriesView,
    MacroSizingCautionView,
    MacroStandingView,
    MacroStateChangeView,
    MacroSynthesisView,
    MacroView,
    MetaBatch,
    MetaView,
    OperationSessionView,
    OperationsView,
    PortfolioOutcomeView,
    PortfolioState,
    PositionReviewView,
    ResearchRevisionView,
    ResearchTriageEntryView,
    ResearchTriageMachineSnapshotView,
    ResearchTriageView,
    ReservationView,
    ReviewSetEntryView,
    ReviewSetView,
    ScenarioView,
    ScreeningHistoryRunView,
    ScreeningRunView,
    ScreeningView,
    SecurityAnalysisRowView,
    SecurityDetailView,
    TaskView,
    ThesisDetailView,
    UpcomingEventView,
    WarningView,
)

_EVENT_WINDOW_DAYS = 14
# Order same-day events so the read most likely to gate an imminent action leads:
# an earnings print, then a reservation lapse.
_EVENT_KIND_ORDER = {"earnings": 0, "reservation_expiry": 1}

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
    "dividend_split_factor",
    "normalized_per_3fy",
    "er_annual",
    "er_reversion_annual",
    "er_carry_annual",
    "net_cash_to_market_cap",
    "fcf_yield",
    "ocf_yield",
    "equity_ratio",
    "sales_yoy",
    "operating_profit_yoy",
    "margin_long_to_adv",
    "margin_short_to_adv",
    "margin_long_share",
    "margin_long_delta_26w",
    "margin_std_long_share",
)

# The deadline-share axis supports this specific flag. Its ninth decile lands at
# 0.93-0.94 of both candidate rows and the liquid population, so one fixed value
# tracks the measured top decile. `margin_short_to_adv` remains a raw annotation
# even after adoption: its production contract does not authorize a warning, gate,
# ranking, FV, E[r], or sizing effect.
_METRIC_TEXT_FIELDS = ("dividend_basis",)

_STALE_RUN_AGE = timedelta(days=7)

# A list row needs enough shape to show direction without restoring the 53k-point
# overview retired by #1152.  Four hundred days covers thirteen monthly buckets even
# around short months and leap years; the explicit cap is the payload contract.
_MACRO_SPARKLINE_LOOKBACK = timedelta(days=400)
_MACRO_SPARKLINE_MAX_POINTS = 13


def build_meta(source: DbMetaSource, *, batch: MetaBatch | None = None) -> MetaView:
    """Report per-store as-of freshness so a consumer can judge view staleness."""

    return MetaView(
        generated_at=datetime.now(_JST),
        data_updated_at=source.data_updated_at(),
        screening_asof=source.screening_asof(),
        macro_asof=source.macro_asof(),
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
    tasks: TaskSource,
    candidates: CandidatesSource,
    market: MarketPriceSource,
) -> DashboardView:
    """Build the app's first view without performing storage I/O directly."""

    now = datetime.now(_JST)
    today = now.date()
    revisions = research.revisions()
    latest_research = _latest_research_by_ticker(revisions)
    latest_run = candidates.latest_run()
    candidate_names = _candidate_names(latest_run)
    open_tasks, next_task = _task_views(tasks.list_tasks(), today=today)
    tasks_exist = tasks.exists()
    research_load_errors = research.load_errors()

    if not ledger.exists():
        return _empty_dashboard(
            generated_at=now,
            ledger_exists=False,
            ledger_error=None,
            open_tasks=open_tasks,
            next_task=next_task,
            tasks_exist=tasks_exist,
            research_load_errors=research_load_errors,
            upcoming_events=_upcoming_events(today=today, holdings=[], reservations=[]),
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
            tasks_exist=tasks_exist,
            research_load_errors=research_load_errors,
            upcoming_events=_upcoming_events(today=today, holdings=[], reservations=[]),
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
    # The page-level basis is conservative: every displayed value is at least this fresh.
    # Individual holding rows retain their exact timestamps when closes are mixed.
    valuation_as_of = (
        min(item.market_price_as_of for item in holdings) if holdings else snapshot.as_of
    )
    return DashboardView(
        generated_at=now,
        ledger_exists=True,
        ledger_error=None,
        ledger_as_of=snapshot.as_of,
        ledger_stale=snapshot.as_of.date() <= today - timedelta(days=7),
        valuation_as_of=valuation_as_of,
        valuation_stale=valuation_as_of.date() <= today - timedelta(days=7),
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
        ),
        open_tasks=open_tasks,
        next_task=next_task,
        tasks_exist=tasks_exist,
        research_load_errors=research_load_errors,
    )


def build_screening(
    candidates: CandidatesSource,
    ledger: LedgerSource,
    research: ResearchSource,
    er_level_calibration: ErLevelCalibrationContext | None = None,
) -> ScreeningView:
    """Build the latest candidates table with portfolio/research annotations."""

    run, review_sets = _operative_run(candidates)
    applicable_calibration = _calibration_for_run(run, er_level_calibration)
    research_triages: list[ResearchTriageView] = []
    assessments: list[CapitalAllocationAssessmentSummaryView] = []
    if isinstance(candidates, DbCandidatesSource):
        research_triages, assessments = _current_screening_judgments(
            candidates,
            review_sets=review_sets,
        )
    if run is None:
        return ScreeningView(
            run=None,
            security_analyses=[],
            review_sets=review_sets,
            research_triages=research_triages,
            capital_allocation_assessments=assessments,
            er_level_calibration=None,
        )
    held, reserved = _held_and_reserved_tickers(ledger)
    researched = {item.ticker for item in research.revisions()}
    today = datetime.now(_JST).date()
    return ScreeningView(
        run=_screening_run_view(run, today=today),
        security_analyses=[
            _candidate_row_view(
                row,
                held=held,
                reserved=reserved,
                researched=researched,
                fair_value=_fair_value_by_ticker(review_sets),
                er_level_calibration=applicable_calibration,
            )
            for row in run.rows
        ],
        review_sets=review_sets,
        research_triages=research_triages,
        capital_allocation_assessments=assessments,
        er_level_calibration=_er_level_calibration_view(applicable_calibration),
    )


def _current_screening_judgments(
    candidates: DbCandidatesSource,
    *,
    review_sets: list[ReviewSetView],
) -> tuple[list[ResearchTriageView], list[CapitalAllocationAssessmentSummaryView]]:
    """Show only the Research Triage -> allocation judgments for the displayed Review Set."""

    review_set_sources = {
        (review_set.review_set_id, review_set.run_revision_id) for review_set in review_sets
    }
    current_triages = [
        item
        for item in candidates.research_triages(
            review_set_ids=[review_set.review_set_id for review_set in review_sets]
        )
        if (str(item.get("review_set_id")), str(item.get("run_revision_id"))) in review_set_sources
    ]
    triage_ids = {str(item["research_triage_id"]) for item in current_triages}
    triage_views = [_research_triage_view(item) for item in current_triages]
    if not triage_ids:
        return triage_views, []
    current_assessments = [
        item
        for item in candidates.assessments()
        if str(item.get("research_triage_id")) in triage_ids
    ]
    return (
        triage_views,
        [_assessment_summary_view(item) for item in current_assessments],
    )


def _calibration_for_run(
    run: CandidatesRun | None,
    context: ErLevelCalibrationContext | None,
) -> ErLevelCalibrationContext | None:
    """Bind historical E[r] bands to the exact method of the displayed run."""

    if run is None or context is None:
        return None
    if (
        run.screening_rules_hash != context.screening_rules_hash
        or run.er_model_version != context.er_model_version
    ):
        return None
    return context


def _operative_run(
    candidates: CandidatesSource,
) -> tuple[CandidatesRun | None, list[ReviewSetView]]:
    """Resolve the run every screening surface reads, with its own review_sets.

    Judgment publications (review_set / research_triage) bind to a specific run revision.
    A newer revision of the same as-of — a determinism re-run, say — carries no
    review_set of its own, so presenting it would blank the machine-review_set view
    and the FV anchors that hang off it. Falling back to the newest review_set's run
    keeps the candidates table, its review_sets, and the research_triage join on one run.

    Every surface that shows a candidate resolves the run here, so the list and the
    security page cannot end up describing the same ticker from different runs.
    """

    run = candidates.latest_run()
    if run is None:
        return None, []
    # Ask for this run's review_sets first: the whole published history is only needed to
    # find a fallback, which is the uncommon case, and a security page reads this on
    # every request.
    run_review_sets = candidates.review_sets(run_revision_id=run.run_revision_id)
    if not run_review_sets and (all_review_sets := candidates.review_sets()):
        newest = max(all_review_sets, key=lambda item: str(item["created_at"]))
        fallback_run = candidates.run(str(newest["run_revision_id"]))
        if fallback_run is not None:
            run = fallback_run
            run_review_sets = [
                item
                for item in all_review_sets
                if str(item["run_revision_id"]) == run.run_revision_id
            ]
    return run, [_machine_review_set_view(item) for item in run_review_sets]


def _fair_value_by_ticker(
    review_sets: list[ReviewSetView],
) -> Mapping[str, ReviewSetEntryView]:
    """FV アンカーを持つのは entries だけなので、その範囲を ticker で引けるようにする。

    複数 review_set が同じ run に束縛される場合は最新の review_set を採る。entries の
    外にいる候補は FV を持たないまま残る。
    """
    if not review_sets:
        return {}
    newest = max(review_sets, key=lambda item: item.created_at)
    return {entry.ticker: entry for entry in newest.entries}


def _review_entry_fair_value(entry: ReviewSetEntryView | None) -> float | None:
    """Project the existing sector-median FV estimate without giving it membership authority."""

    if entry is None:
        return None
    expected = entry.analysis.get("expected_return")
    return _number(expected.get("fv_sector_median_yen")) if isinstance(expected, Mapping) else None


def build_screening_history_run(
    candidates: DbCandidatesSource,
    ledger: LedgerSource,
    research: ResearchSource,
    *,
    as_of: date,
) -> ScreeningHistoryRunView | None:
    """Build one retained run with the same current-state annotations as latest."""

    run = candidates.run_as_of(as_of)
    if run is None:
        return None
    held, reserved = _held_and_reserved_tickers(ledger)
    researched = {item.ticker for item in research.revisions()}
    fair_value = _fair_value_by_ticker(
        [
            _machine_review_set_view(item)
            for item in candidates.review_sets(run_revision_id=run.run_revision_id)
        ]
    )
    return ScreeningHistoryRunView(
        run=_screening_run_view(run, today=datetime.now(_JST).date()),
        rows=[
            _candidate_row_view(
                row,
                held=held,
                reserved=reserved,
                researched=researched,
                fair_value=fair_value,
            )
            for row in run.rows
        ],
    )


def build_assessment_detail(
    candidates: DbCandidatesSource,
    *,
    capital_allocation_assessment_id: str,
) -> CapitalAllocationAssessmentView | None:
    """Build one published capital-allocation assessment for the detail page."""

    raw = candidates.assessment(capital_allocation_assessment_id)
    if raw is None:
        return None
    return _assessment_view(raw)


def _assessment_summary_view(raw: Mapping[str, object]) -> CapitalAllocationAssessmentSummaryView:
    alternatives = _mapping_items_optional(raw.get("alternatives"))
    allocated = next(
        (
            str(alternative["ticker"])
            for alternative in alternatives
            if alternative.get("disposition") == "allocate"
        ),
        None,
    )
    return CapitalAllocationAssessmentSummaryView(
        capital_allocation_assessment_id=str(raw["capital_allocation_assessment_id"]),
        as_of=date.fromisoformat(str(raw["as_of"])),
        published_at=datetime.fromisoformat(str(raw["published_at"])),
        result=str(raw["result"]),
        headline=str(raw["headline"]),
        research_triage_id=str(raw["research_triage_id"]),
        alternative_count=len(alternatives),
        allocated_ticker=allocated,
    )


def _assessment_view(
    raw: Mapping[str, object],
) -> CapitalAllocationAssessmentView:
    return CapitalAllocationAssessmentView(
        capital_allocation_assessment_id=str(raw["capital_allocation_assessment_id"]),
        as_of=date.fromisoformat(str(raw["as_of"])),
        published_at=datetime.fromisoformat(str(raw["published_at"])),
        result=str(raw["result"]),
        headline=str(raw["headline"]),
        research_triage_id=str(raw["research_triage_id"]),
        macro_context_id=_text(raw.get("macro_context_id")),
        comparison=str(raw["comparison"]),
        forgone=str(raw["forgone"]),
        alternatives=[
            _allocation_alternative_view(item)
            for item in _mapping_items_optional(raw.get("alternatives"))
        ],
        review=AssessmentReviewView.model_validate(raw["review"]),
    )


def _allocation_alternative_view(raw: Mapping[str, object]) -> AllocationAlternativeView:
    projection = raw.get("thesis_projection")
    machine_values = projection if isinstance(projection, Mapping) else {}
    return AllocationAlternativeView(
        ticker=str(raw["ticker"]),
        disposition=str(raw["disposition"]),
        rationale=str(raw["rationale"]),
        thesis_id=str(raw["thesis_id"]),
        thesis_review_id=_text(raw.get("thesis_review_id")),
        permanent_loss_conclusion=_text(machine_values.get("permanent_loss_conclusion")),
        five_year_base_cagr_pct=_number(machine_values.get("five_year_base_cagr_pct")),
        fair_value_yen=_number(machine_values.get("fair_value_yen")),
        fv_gap_pct=_number(machine_values.get("fv_gap_pct")),
    )


def _machine_review_set_view(raw: Mapping[str, object]) -> ReviewSetView:
    payload = raw.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("machine review_set payload must be an object")
    return ReviewSetView(
        review_set_id=str(raw["review_set_id"]),
        run_revision_id=str(raw["run_revision_id"]),
        created_at=datetime.fromisoformat(str(raw["created_at"])),
        entries=[
            _review_set_entries_entry_view(item)
            for item in _mapping_items_optional(payload.get("entries"))
        ],
    )


def _review_set_entries_entry_view(raw: Mapping[str, object]) -> ReviewSetEntryView:
    nominations = _mapping_items_optional(raw.get("nominations"))
    analysis = raw.get("analysis")
    if not isinstance(analysis, Mapping):
        raise ValueError("Review Set entry analysis must be an object")
    rank_vector = raw.get("rank_vector")
    if not isinstance(rank_vector, list) or not all(isinstance(item, int) for item in rank_vector):
        raise ValueError("Review Set rank_vector must be an integer array")
    return ReviewSetEntryView(
        review_position=_required_int(raw["review_position"], field="review_position"),
        ticker=str(raw.get("ticker", "")),
        name=_text(raw.get("name")),
        sector_33=_text(raw.get("sector_33")),
        nominations=[dict(item) for item in nominations],
        support_count=_required_int(raw["support_count"], field="support_count"),
        rank_vector=[int(item) for item in rank_vector],
        analysis=dict(analysis),
    )


def _fv_convergence_view(raw: object) -> FvConvergenceView:
    payload = raw if isinstance(raw, Mapping) else {}
    status = payload.get("status")
    if status not in {"warning", "clear", "not_evaluable"}:
        status = "not_evaluable"
    anchors = payload.get("anchors_yen")
    return FvConvergenceView(
        status=status,
        warning_code=_text(payload.get("warning_code")),
        market_price_yen=_number(payload.get("market_price_yen")),
        anchors_yen={
            str(key): number
            for key, value in (anchors.items() if isinstance(anchors, Mapping) else ())
            if (number := _number(value)) is not None and number > 0
        },
        er_reversion_annual=_number(payload.get("er_reversion_annual")),
    )


def _fair_value_gap_pct(anchor: float | None, price: float | None) -> float | None:
    """FV アンカーと screening 参考価格の乖離率。導出は read model 側で 1 回だけ行う。"""
    if anchor is None or price is None or price == 0:
        return None
    return round((anchor / price - 1) * 100, 4)


def _required_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    return value


def _research_triage_entry_view(raw: Mapping[str, object]) -> ResearchTriageEntryView:
    """判断 1 件と、publish時に焼き込まれた部分的な機械座標を読む。"""

    snapshot = raw.get("machine_snapshot")
    view = ResearchTriageEntryView.model_validate({**raw, "machine_snapshot": None})
    if not isinstance(snapshot, Mapping):
        return view
    return view.model_copy(
        update={"machine_snapshot": ResearchTriageMachineSnapshotView.model_validate(snapshot)}
    )


def _research_triage_view(raw: Mapping[str, object]) -> ResearchTriageView:
    entries: list[ResearchTriageEntryView] = []
    unreadable = 0
    for item in _mapping_items(raw.get("entries")):
        try:
            entries.append(_research_triage_entry_view(item))
        except ValidationError:
            # 発行済み revision は immutable なので、read 経路が形の違いで落ちると
            # export ごと止まる。読めない entry は数えて面へ出し、黙って消さない。
            unreadable += 1
    return ResearchTriageView(
        research_triage_id=str(raw["research_triage_id"]),
        review_set_id=str(raw["review_set_id"]),
        run_revision_id=str(raw["run_revision_id"]),
        as_of=date.fromisoformat(str(raw["as_of"])),
        published_at=datetime.fromisoformat(str(raw["published_at"])),
        entries=entries,
        unreadable_entries=unreadable,
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

    # One "today" for the whole page: the earnings lookup and the run staleness badge
    # would otherwise straddle midnight and contradict each other within one response.
    today = datetime.now(_JST).date()
    revisions = [item for item in research.revisions() if item.ticker == ticker]
    latest_revision = revisions[0] if revisions else None
    run, review_sets = _operative_run(candidates)
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
            next_earnings_date=market.next_earnings_dates([ticker], asof=today).get(ticker),
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
            fair_value=_fair_value_by_ticker(review_sets),
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
        position_reviews=[
            PositionReviewView(
                position_review_id=item.position_review_id,
                as_of=item.as_of,
                thesis_id=item.thesis_id,
                candidate_thesis_id=item.candidate_thesis_id,
                action=item.action,
                note=item.note,
            )
            for item in research.position_reviews(ticker=ticker)
        ],
        candidate_row=candidate_row,
        candidate_run=(_screening_run_view(run, today=today) if run is not None else None),
    )


def build_macro(
    source: DbMacroSource,
    *,
    as_of: date,
) -> MacroView:
    """Build the daily entrance with bounded sparklines, never full indicator history."""

    latest_context_raw = source.latest_context(as_of=as_of)
    context_asof = (
        None if latest_context_raw is None else date.fromisoformat(str(latest_context_raw["as_of"]))
    )
    daily = source.daily_readings(requested_asof=as_of, context_asof=context_asof)
    fetch_health = source.fetch_health()
    current_payload = _optional_mapping(None if daily is None else daily.get("current"))
    previous_payload = _optional_mapping(None if daily is None else daily.get("previous"))
    context_payload = _optional_mapping(None if daily is None else daily.get("context"))
    reading = (
        None
        if current_payload is None
        else MacroReadingView.model_validate({**current_payload, "fetch_health": fetch_health})
    )
    latest_context_id = (
        None if latest_context_raw is None else str(latest_context_raw["context_id"])
    )
    reports = [
        MacroContextRevisionView(
            context_id=str(item["context_id"]),
            as_of=(report_asof := date.fromisoformat(str(item["as_of"]))),
            published_at=datetime.fromisoformat(str(item["published_at"])),
            summary=str(item["summary"]),
            age_days=(age_days := (as_of - report_asof).days),
            stale=age_days > MACRO_CONTEXT_STALE_DAYS,
        )
        for item in source.contexts()
        if str(item["context_id"]) != latest_context_id
    ]
    return MacroView(
        requested_as_of=as_of,
        data_as_of=_optional_iso_date(None if daily is None else daily.get("data_as_of")),
        previous_data_as_of=_optional_iso_date(
            None if daily is None else daily.get("previous_data_as_of")
        ),
        context_as_of=context_asof,
        rules_revision=None if daily is None else str(daily["rules_revision"]),
        machine_update=_macro_machine_update(
            current=current_payload,
            previous=previous_payload,
            context=context_payload,
            context_asof=context_asof,
            fetch_health=fetch_health,
        ),
        latest_context=(
            None
            if latest_context_raw is None
            else _macro_context_excerpt(latest_context_raw, as_of=as_of)
        ),
        reading=reading,
        reports=reports,
        groups=_build_macro_group_metadata(source, as_of=as_of),
    )


def _macro_machine_update(
    *,
    current: Mapping[str, object] | None,
    previous: Mapping[str, object] | None,
    context: Mapping[str, object] | None,
    context_asof: date | None,
    fetch_health: list[dict[str, object]],
) -> MacroMachineUpdateView:
    current_by_series = {} if current is None else _reading_by_series(current)
    failed = [
        MacroSeriesFetchHealthView.model_validate(item)
        for item in fetch_health
        if item.get("status") == "failed"
    ]
    stale = sorted(
        series_id for series_id, item in current_by_series.items() if item.get("stale") is True
    )
    flagged = sorted(series_id for series_id, item in current_by_series.items() if _flag_set(item))
    extremes = sorted(
        series_id
        for series_id, item in current_by_series.items()
        if (z_score := _number(item.get("z_score"))) is not None
        and abs(z_score) >= _DELTA_MACRO_Z_EDGE
    )
    return MacroMachineUpdateView(
        series_total=len(current_by_series),
        fetch_ok_count=sum(item.get("status") == "ok" for item in fetch_health),
        fetch_failed_count=len(failed),
        previous_day=_macro_comparison(current=current, earlier=previous),
        since_context=(
            None
            if context_asof is None or current is None
            else _macro_comparison(current=current, earlier=context)
        ),
        standing=MacroStandingView(
            fetch_failed=failed,
            stale_series_ids=stale,
            flagged_series_ids=flagged,
            extreme_series_ids=extremes,
        ),
    )


def _macro_comparison(
    *,
    current: Mapping[str, object] | None,
    earlier: Mapping[str, object] | None,
) -> MacroComparisonView | None:
    if current is None or earlier is None:
        return None
    current_series = _reading_by_series(current)
    earlier_series = _reading_by_series(earlier)
    changes: list[MacroSeriesChangeView] = []
    states: list[MacroStateChangeView] = []
    for series_id in sorted(set(current_series) | set(earlier_series)):
        now = current_series.get(series_id, {})
        before = earlier_series.get(series_id, {})
        if (now.get("latest_value"), now.get("observed_at")) != (
            before.get("latest_value"),
            before.get("observed_at"),
        ):
            now_value = _number(now.get("latest_value"))
            before_value = _number(before.get("latest_value"))
            now_z = _number(now.get("z_score"))
            before_z = _number(before.get("z_score"))
            changes.append(
                MacroSeriesChangeView(
                    series_id=series_id,
                    name=str(now.get("name") or before.get("name") or series_id),
                    frequency=str(now.get("frequency") or before.get("frequency") or ""),
                    unit=str(now.get("unit") or before.get("unit") or ""),
                    previous_observed_at=_optional_iso_date(before.get("observed_at")),
                    observed_at=_optional_iso_date(now.get("observed_at")),
                    previous_value=before_value,
                    value=now_value,
                    value_change=(
                        None
                        if now_value is None or before_value is None
                        else now_value - before_value
                    ),
                    z_score_delta=(None if now_z is None or before_z is None else now_z - before_z),
                )
            )
        now_flags = _flag_set(now)
        before_flags = _flag_set(before)
        states.extend(
            MacroStateChangeView(series_id=series_id, kind="flag", state="raised", detail=flag)
            for flag in sorted(now_flags - before_flags)
        )
        states.extend(
            MacroStateChangeView(series_id=series_id, kind="flag", state="cleared", detail=flag)
            for flag in sorted(before_flags - now_flags)
        )
        if bool(now.get("stale")) != bool(before.get("stale")):
            states.append(
                MacroStateChangeView(
                    series_id=series_id,
                    kind="stale",
                    state="raised" if bool(now.get("stale")) else "cleared",
                    detail="stale",
                )
            )
        now_z = _number(now.get("z_score"))
        before_z = _number(before.get("z_score"))
        now_extreme = now_z is not None and abs(now_z) >= _DELTA_MACRO_Z_EDGE
        before_extreme = before_z is not None and abs(before_z) >= _DELTA_MACRO_Z_EDGE
        if now_extreme != before_extreme:
            states.append(
                MacroStateChangeView(
                    series_id=series_id,
                    kind="extreme",
                    state="raised" if now_extreme else "cleared",
                    detail=f"|z| >= {_DELTA_MACRO_Z_EDGE:g}",
                )
            )
    daily = [item for item in changes if item.frequency == "daily"]
    daily.sort(
        key=lambda item: (
            -(abs(item.z_score_delta) if item.z_score_delta is not None else -1.0),
            item.series_id,
        )
    )
    displayed_daily = daily[:_MACRO_DAILY_MOVE_LIMIT]
    return MacroComparisonView(
        from_as_of=date.fromisoformat(str(earlier["asof"])),
        to_as_of=date.fromisoformat(str(current["asof"])),
        changed_total=len(changes),
        daily_changed_total=len(daily),
        daily_moves=displayed_daily,
        daily_moves_omitted=len(daily) - len(displayed_daily),
        non_daily_updates=[item for item in changes if item.frequency != "daily"],
        state_changes=states,
    )


def _macro_context_excerpt(
    raw_context: Mapping[str, object], *, as_of: date
) -> MacroContextExcerptView:
    context = _build_macro_context_view(
        raw_context,
        as_of=as_of,
        series_names=macro_series_names(),
    )
    risk_section = next(
        (section for section in context.core if section.section_id == "risk_environment"),
        None,
    )
    warnings = ["context_stale"] if context.stale else []
    raw_inputs = raw_context.get("inputs")
    if isinstance(raw_inputs, Mapping):
        for input_group in (
            "articles",
            "indicator_series",
            "reading_snapshots",
            "machine_snapshots",
        ):
            items = raw_inputs.get(input_group)
            if not isinstance(items, list):
                continue
            warnings.extend(
                f"failed_input:{item.get('input_id')}"
                for item in items
                if isinstance(item, Mapping) and item.get("status") == "failed"
            )
    return MacroContextExcerptView(
        context_id=context.context_id,
        as_of=context.as_of,
        published_at=context.published_at,
        age_days=context.age_days,
        stale=context.stale,
        summary=context.summary,
        synthesis=context.synthesis,
        risk_environment=None if risk_section is None else risk_section.risk_environment,
        scenarios=[] if risk_section is None else risk_section.scenarios,
        material_deltas=[delta for section in context.core for delta in section.material_deltas],
        research_priority_hints=context.connection.research_priority_hints,
        bargain_topography=context.connection.bargain_topography,
        estimate_caveats=context.connection.estimate_caveats,
        sizing_cautions=context.connection.sizing_cautions,
        warnings=warnings,
    )


def _optional_mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _optional_iso_date(value: object) -> date | None:
    return None if value is None else date.fromisoformat(str(value))


def build_macro_context_detail(
    source: DbMacroSource,
    *,
    context_id: str,
    as_of: date,
) -> MacroContextView:
    """Build one published macro report (core 10 + connection) for the detail page."""
    raw_context = source.context_by_id(context_id=context_id, as_of=as_of)
    return _build_macro_context_view(
        raw_context,
        as_of=as_of,
        series_names=macro_series_names(),
    )


def _build_macro_context_view(
    raw_context: Mapping[str, object],
    *,
    as_of: date,
    series_names: Mapping[str, str],
) -> MacroContextView:
    context_as_of = date.fromisoformat(str(raw_context["as_of"]))
    age_days = (as_of - context_as_of).days
    connection = raw_context["connection"]
    if not isinstance(connection, Mapping):
        raise ValueError("macro context connection must be an object")
    return MacroContextView(
        context_id=str(raw_context["context_id"]),
        as_of=context_as_of,
        published_at=datetime.fromisoformat(str(raw_context["published_at"])),
        summary=str(raw_context["summary"]),
        age_days=age_days,
        stale=age_days > MACRO_CONTEXT_STALE_DAYS,
        synthesis=_macro_synthesis_view(raw_context.get("synthesis"), series_names=series_names),
        core=[
            _macro_core_section_view(item, series_names=series_names)
            for item in _mapping_items(raw_context["core"])
        ],
        connection=_macro_connection_section_view(connection, series_names=series_names),
    )


def _macro_synthesis_view(
    raw: object, *, series_names: Mapping[str, str]
) -> MacroSynthesisView | None:
    # Revisions published before the integrated layer carry no synthesis key.
    if not isinstance(raw, Mapping):
        return None
    return MacroSynthesisView(
        dominant_forces=[
            MacroDominantForceView(
                force_id=str(item["force_id"]),
                title=str(item["title"]),
                summary=str(item["summary"]),
                transmission=str(item["transmission"]),
                core_section_ids=_string_items(item.get("core_section_ids")),
                series=_macro_series_references(item, series_names=series_names),
                counter_evidence=str(item["counter_evidence"]),
                direction=str(item["direction"]),
                confidence=str(item["confidence"]),
                source_ids=_string_items(item.get("source_ids")),
            )
            for item in _mapping_items(raw.get("dominant_forces"))
        ],
        interactions=[
            MacroForceInteractionView.model_validate(item)
            for item in _mapping_items(raw.get("interactions"))
        ],
    )


def _build_macro_group_metadata(source: DbMacroSource, *, as_of: date) -> list[MacroGroupView]:
    """Return configured rows with a fixed, bounded monthly sparkline sample."""

    groups: list[MacroGroupView] = []
    for group in source.groups:
        groups.append(
            MacroGroupView(
                title=group.title,
                series=[
                    _build_macro_sparkline_series(source, configured=item, as_of=as_of)
                    for item in group.series
                ],
            )
        )
    return groups


def _build_macro_sparkline_series(
    source: DbMacroSource,
    *,
    configured: MacroSeriesConfig,
    as_of: date,
) -> MacroSeriesView:
    raw_series = source.series(
        configured.series_id,
        start=as_of - _MACRO_SPARKLINE_LOOKBACK,
        end=as_of,
        granularity="monthly",
    )
    if raw_series is None:
        return _unfetched_macro_series_view(configured)
    return _macro_series_view(
        configured,
        raw_series,
        points_limit=_MACRO_SPARKLINE_MAX_POINTS,
    )


def build_macro_series(
    source: DbMacroSource,
    *,
    series_id: str,
    as_of: date,
) -> MacroSeriesView | None:
    """Materialize the full daily history of exactly one configured series."""

    configured = next(
        (item for group in source.groups for item in group.series if item.series_id == series_id),
        None,
    )
    if configured is None:
        return None
    raw_series = source.series(
        series_id,
        start=None,
        end=as_of,
        granularity="daily",
    )
    if raw_series is None:
        return _unfetched_macro_series_view(configured)
    return _macro_series_view(configured, raw_series)


def _macro_series_view(
    configured: MacroSeriesConfig,
    raw_series: Mapping[str, object],
    *,
    points_limit: int | None = None,
) -> MacroSeriesView:
    name = str(raw_series["name"])
    points = [MacroPointView.model_validate(item) for item in _mapping_items(raw_series["points"])]
    if points_limit is not None:
        points = points[-points_limit:]
    return MacroSeriesView(
        series_id=configured.series_id,
        label=configured.label or name,
        name=name,
        unit=str(raw_series["unit"]),
        tradingview_symbol=(
            str(raw_series["tradingview_symbol"])
            if raw_series.get("tradingview_symbol") is not None
            else None
        ),
        points=points,
    )


def _unfetched_macro_series_view(configured: MacroSeriesConfig) -> MacroSeriesView:
    """A configured series the indicator store does not carry, shown as having no data.

    The registry defines a series before any store fetches it, so a store that has not
    caught up is a freshness state: the panel keeps the configured row and the card reads
    as having no observations. An id the registry does not define is configuration drift
    and fails the build rather than rendering as permanently empty.
    """

    registered = macro_registered_series(configured.series_id)
    if registered is None:
        raise ValueError(f"configured macro series is not registered: {configured.series_id}")
    name = str(registered["name"])
    return MacroSeriesView(
        series_id=configured.series_id,
        label=configured.label or name,
        name=name,
        unit=str(registered["unit"]),
        tradingview_symbol=registered["tradingview_symbol"],
        points=[],
    )


def _mapping_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError("expected an array of objects")
    return [item for item in value if isinstance(item, Mapping)]


def _macro_core_section_view(
    raw: Mapping[str, object], *, series_names: Mapping[str, str]
) -> MacroCoreSectionView:
    return MacroCoreSectionView(
        section_id=str(raw["section_id"]),
        series=_macro_series_references(raw, series_names=series_names),
        fact_summary=[
            MacroFactSummaryView.model_validate(item)
            for item in _mapping_items(raw.get("fact_summary"))
        ],
        judgment=MacroSectionJudgmentView.model_validate(raw["judgment"]),
        economic_connection=MacroEconomicConnectionView.model_validate(raw["economic_connection"]),
        change_since_previous=_optional_text(raw.get("change_since_previous")),
        previous_scorecard_review=_optional_text(raw.get("previous_scorecard_review")),
        material_deltas=[
            MacroMaterialDeltaView.model_validate(item)
            for item in _mapping_items(raw.get("material_deltas"))
        ],
        risk_environment=(
            MacroRiskEnvironmentView.model_validate(raw["risk_environment"])
            if raw.get("risk_environment") is not None
            else None
        ),
        scenarios=[
            MacroScenarioView.model_validate(item) for item in _mapping_items(raw.get("scenarios"))
        ],
        monitoring_points=[
            MacroMonitoringPointView.model_validate(item)
            for item in _mapping_items(raw.get("monitoring_points"))
        ],
    )


def _macro_connection_section_view(
    raw: Mapping[str, object], *, series_names: Mapping[str, str]
) -> MacroConnectionSectionView:
    return MacroConnectionSectionView(
        section_id=str(raw["section_id"]),
        series=_macro_series_references(raw, series_names=series_names),
        core_section_ids=_string_items(raw.get("core_section_ids")),
        fact_summary=[
            MacroFactSummaryView.model_validate(item)
            for item in _mapping_items(raw.get("fact_summary"))
        ],
        judgment=MacroSectionJudgmentView.model_validate(raw["judgment"]),
        research_priority_hints=[
            MacroResearchPriorityHintView.model_validate(item)
            for item in _mapping_items(raw.get("research_priority_hints"))
        ],
        sector_tilts=[
            MacroSectorTiltView.model_validate(item)
            for item in _mapping_items(raw.get("sector_tilts"))
        ],
        sizing_cautions=[
            MacroSizingCautionView.model_validate(item)
            for item in _mapping_items(raw.get("sizing_cautions"))
        ],
        bargain_topography=(
            MacroFactSummaryView.model_validate(raw["bargain_topography"])
            if isinstance(raw.get("bargain_topography"), Mapping)
            else None
        ),
        estimate_caveats=[
            MacroEstimateCaveatView.model_validate(item)
            # Revisions published before the integrated layer carry no caveats key.
            for item in _mapping_items(raw.get("estimate_caveats") or [])
        ],
    )


def _macro_series_references(
    raw: Mapping[str, object], *, series_names: Mapping[str, str]
) -> list[MacroSeriesReferenceView]:
    return [
        MacroSeriesReferenceView(series_id=series_id, name=series_names.get(series_id, series_id))
        for series_id in _string_items(raw.get("series_ids"))
    ]


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


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
        upcoming_events=upcoming_events,
        open_tasks=open_tasks,
        next_task=next_task,
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
    # The app does not lag stale ledger prices. Anything not newer keeps the ledger value.
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


def _format_market_price(value: float) -> str:
    """Render a market close like the ledger's decimal price, dropping a bare .0."""

    return str(int(value)) if value.is_integer() else str(value)


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
        analyzed_security_count=len(run.rows),
        run_revision_id=run.run_revision_id,
        stale=run.asof_date <= today - _STALE_RUN_AGE,
    )


def _candidate_row_view(
    row: Mapping[str, object],
    *,
    held: set[str],
    reserved: set[str],
    researched: set[str],
    fair_value: Mapping[str, ReviewSetEntryView] | None = None,
    er_level_calibration: ErLevelCalibrationContext | None = None,
) -> SecurityAnalysisRowView:
    ticker = str(row.get("ticker", ""))
    metrics_raw = row.get("metrics")
    metrics = metrics_raw if isinstance(metrics_raw, Mapping) else {}
    values = {name: _number(row.get(name)) for name in _NUMERIC_FIELDS}
    values.update({name: _number(metrics.get(name)) for name in _METRIC_FIELDS})
    # 文字列の annotation は `_number` を通すと必ず None になるので別に詰める。
    # `dividend_basis` は「配当利回りが空である理由」を持つ唯一の field で、
    # unresolved_split_basis を無配と読み違えないために候補表まで届ける必要がある。
    text_values = {name: _text(metrics.get(name)) for name in _METRIC_TEXT_FIELDS}
    flags = _data_quality_flags(row, metrics)
    review_entry = (fair_value or {}).get(ticker)
    anchor = _review_entry_fair_value(review_entry)
    return SecurityAnalysisRowView(
        ticker=ticker,
        name=_text(row.get("name")),
        sector_33=_text(row.get("sector_33")),
        next_earnings_date=_text(row.get("next_earnings_date")),
        margin_week_end=_text(metrics.get("margin_week_end")),
        data_quality_flags=flags,
        portfolio_state=_portfolio_state(ticker, held=held, reserved=reserved),
        has_research=ticker in researched,
        fair_value_anchor_yen=anchor,
        fair_value_gap_pct=None,
        er_level_quintile=_er_level_quintile(values.get("er_annual"), er_level_calibration),
        er_meets_8_5pct_band=_er_meets_hurdle(values.get("er_annual"), er_level_calibration),
        **values,
        **text_values,
    )


def _er_level_quintile(
    er_annual: float | None, context: ErLevelCalibrationContext | None
) -> int | None:
    if er_annual is None or context is None:
        return None
    horizon = next(
        (item for item in context.horizons if item.horizon == context.reference_horizon), None
    )
    if horizon is None:
        return None
    for band in horizon.bands:
        if band.quintile is not None and (
            band.upper_er_annual is None or er_annual <= band.upper_er_annual
        ):
            return band.quintile
    return None


def _er_meets_hurdle(er_annual: float | None, context: ErLevelCalibrationContext | None) -> bool:
    if er_annual is None or context is None:
        return False
    horizon = next(
        (item for item in context.horizons if item.horizon == context.reference_horizon), None
    )
    if horizon is None:
        return False
    hurdle = next((item for item in horizon.bands if item.band_id == "er_gte_8_5pct"), None)
    return bool(
        hurdle is not None
        and hurdle.lower_er_annual is not None
        and er_annual >= hurdle.lower_er_annual
    )


def _er_level_calibration_view(
    context: ErLevelCalibrationContext | None,
) -> ErLevelCalibrationContextView | None:
    if context is None:
        return None
    return ErLevelCalibrationContextView(
        generated_at=context.generated_at,
        valid_through=context.valid_through,
        reference_horizon=context.reference_horizon,
        screening_rules_hash=context.screening_rules_hash,
        er_model_version=context.er_model_version,
        primary_realized_basis=context.primary_realized_basis,
        secondary_realized_basis=context.secondary_realized_basis,
        trap_basis=context.trap_basis,
        horizons=[
            ErLevelCalibrationHorizonView(
                horizon=item.horizon,
                asof_start=item.asof_start,
                asof_end=item.asof_end,
                cohort_count=item.cohort_count,
                bands=[
                    ErLevelCalibrationBandView(
                        band_id=band.band_id,
                        quintile=band.quintile,
                        lower_er_annual=band.lower_er_annual,
                        upper_er_annual=band.upper_er_annual,
                        median_predicted_er_annual=band.median_predicted_er_annual,
                        cohort_count=band.cohort_count,
                        median_n=band.median_n,
                        bases=[
                            ErLevelCalibrationBasisView(
                                basis=basis.basis,
                                ticker_equal=ErLevelCalibrationStatsView(
                                    median=basis.ticker_equal.median,
                                    q25=basis.ticker_equal.q25,
                                    q10=basis.ticker_equal.q10,
                                    trap_rate=basis.ticker_equal.trap_rate,
                                    n=basis.ticker_equal.n,
                                ),
                                cohort_equal=ErLevelCalibrationStatsView(
                                    median=basis.cohort_equal.median,
                                    q25=basis.cohort_equal.q25,
                                    q10=basis.cohort_equal.q10,
                                    trap_rate=basis.cohort_equal.trap_rate,
                                    n=basis.cohort_equal.n,
                                ),
                            )
                            for basis in band.bases
                        ],
                    )
                    for band in item.bands
                ],
            )
            for item in context.horizons
        ],
    )


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


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


# How many movers or pool changes to report. The reader acts on a handful; a full
# ranking is what the Candidates table already is.
_DELTA_ROWS_SHOWN = 10
_DELTA_ER_MOVERS_SHOWN = 5
# Report a machine E[r] move only past this size in percentage points. Measured on
# the live store, adjacent runs move 30-40 names by at least 1pp and 3-4 by at least
# this much, so a lower bar would leave the row cap doing all the review_set and drop
# the rest without saying so.
_DELTA_ER_MOVE_MIN_PP = 3.0
# Report a holding's move only past this size. Daily noise is not a change worth a
# reader's attention; a move this large is.
_DELTA_HOLDING_MOVE_MIN_PCT = 5.0
# The distribution edge the macro reading itself treats as the edge, and the band a
# series has to come from to count as newly arriving there. Measured on the live
# store, 45 sessions produced 8 crossings and every one came from 2.86-2.99 — the
# same series oscillating across a single line, not a series reaching the edge.
_DELTA_MACRO_Z_EDGE = 3.0
# Twelve keeps the busiest observed day (35 changed daily series) to one scannable
# desktop block and a bounded mobile scroll. The total/omitted counts preserve the
# complete fact, and the full current-reading table remains directly below it.
_MACRO_DAILY_MOVE_LIMIT = 12
# The run's Review Set is the review population. Its candidate array is the whole
# evaluated universe, so comparing that would report listings and delistings rather
# than bargains appearing.


def build_daily_delta(
    candidates: CandidatesSource,
    ledger: LedgerSource,
    research: ResearchSource,
    market: MarketPriceSource,
) -> DailyDeltaView:
    """Compare the latest machine run with the one before it.

    The view exists so that a change does not wait for someone to go looking. It
    reports observations only: which tickers entered or left the machine pool, which
    machine estimates moved, and which holdings stand at or above their recorded fair
    value. Whether any of that is worth a research cycle or a Position Review is the
    reader's call. Macro changes belong to the dedicated daily brief.

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
    latest = candidates.latest_run()
    previous = candidates.previous_run()
    if latest is None:
        unavailable.append("candidates")
    elif previous is None:
        unavailable.append("candidates_previous_run")

    pool: DeltaPool | None = None
    rules_changed = False
    entered: list[CandidateEntryDeltaView] = []
    exited: list[CandidateEntryDeltaView] = []
    er_moves: list[CandidateMoveDeltaView] = []
    er_moves_total = 0
    if latest is not None and previous is not None:
        rules_changed = (
            latest.rules_ref is not None
            and previous.rules_ref is not None
            and latest.rules_ref != previous.rules_ref
        )
        pools = _delta_pools(candidates, latest, previous)
        if pools is None:
            unavailable.append("candidates_pool")
        elif rules_changed:
            # A rules revision replaces the pool wholesale, so the difference is a
            # method change and not a market change. Naming it is the honest answer.
            pool = pools[0]
        else:
            pool, current_pool, previous_pool = pools
            if current_pool and not any(_pool_er(row) is not None for row in current_pool.values()):
                # A pool whose rows carry no estimate cannot produce a mover, and an
                # empty mover list would read as "nothing moved". Naming it keeps a
                # pool shape this reader does not know from silencing the section.
                unavailable.append("candidates_estimate")
            entered, exited, er_moves, er_moves_total = _candidate_deltas(
                current_pool,
                previous_pool,
                market=market if market_ready else None,
                previous_asof=previous.asof_date,
            )

    holdings: list[HoldingDeltaView] = []
    holdings_without_fair_value = 0
    holdings_without_price = 0
    if not ledger.exists():
        unavailable.append("holdings")
    elif market_ready:
        if research.load_errors():
            # A thesis the store cannot read is not a holding without a fair value.
            unavailable.append("holdings_fair_value")
        holdings, holdings_without_fair_value, holdings_without_price = _holding_deltas(
            ledger.snapshot(),
            research=research,
            market=market,
            asof=today,
            previous_asof=None if previous is None else previous.asof_date,
        )

    return DailyDeltaView(
        generated_at=now,
        asof=None if latest is None else latest.asof_date,
        previous_asof=None if previous is None else previous.asof_date,
        pool=pool,
        rules_changed=rules_changed,
        entered=entered,
        exited=exited,
        er_moves=er_moves,
        er_moves_total=er_moves_total,
        holdings=holdings,
        holdings_without_fair_value=holdings_without_fair_value,
        holdings_without_price=holdings_without_price,
        unavailable=sorted(dict.fromkeys(unavailable)),
    )


def _pool_rows(
    payloads: list[dict[str, object]], name: str
) -> dict[str, Mapping[str, object]] | None:
    """Index one pool of a run's machine review_set by ticker, or None when absent."""

    for payload in payloads:
        body = payload.get("payload")
        rows = body.get(name) if isinstance(body, Mapping) else None
        if not isinstance(rows, list):
            continue
        indexed = {
            str(row.get("ticker")): row
            for row in rows
            if isinstance(row, Mapping) and row.get("ticker")
        }
        if len(indexed) != len(rows):
            return None
        return indexed
    return None


def _delta_pools(
    candidates: CandidatesSource, latest: CandidatesRun, previous: CandidatesRun
) -> tuple[DeltaPool, dict[str, Mapping[str, object]], dict[str, Mapping[str, object]]] | None:
    """Pick the most informative pool both runs published, or None when neither did.

    What counts as a candidate is decided by ``select`` and published in its output;
    re-deriving it here from the run's candidate array would both duplicate the screen
    definition and compare the whole universe.
    """

    latest_payloads = candidates.review_sets(run_revision_id=latest.run_revision_id)
    previous_payloads = candidates.review_sets(run_revision_id=previous.run_revision_id)
    current = _pool_rows(latest_payloads, "entries")
    earlier = _pool_rows(previous_payloads, "entries")
    if current is None or earlier is None:
        return None
    return "review_set", current, earlier


def _pool_er(row: Mapping[str, object]) -> float | None:
    """Read contextual E[r] from a Review Set entry."""

    analysis = row.get("analysis")
    expected = analysis.get("expected_return") if isinstance(analysis, Mapping) else None
    return _number(expected.get("er_annual")) if isinstance(expected, Mapping) else None


def _candidate_entry_delta(
    row: Mapping[str, object], *, disclosed: bool | None
) -> CandidateEntryDeltaView:
    return CandidateEntryDeltaView(
        ticker=str(row.get("ticker", "")),
        company_name=_text(row.get("name")),
        er_annual_pct=_percent(_pool_er(row)),
        disclosed_since_previous=disclosed,
    )


def _candidate_deltas(
    current: Mapping[str, Mapping[str, object]],
    earlier: Mapping[str, Mapping[str, object]],
    *,
    market: MarketPriceSource | None,
    previous_asof: date,
) -> tuple[
    list[CandidateEntryDeltaView],
    list[CandidateEntryDeltaView],
    list[CandidateMoveDeltaView],
    int,
]:
    def by_er(tickers: set[str], rows: Mapping[str, Mapping[str, object]]) -> list[str]:
        # Value order, so a capped list keeps the names worth reading first.
        return sorted(tickers, key=lambda ticker: (-(_pool_er(rows[ticker]) or 0.0), ticker))

    entered_tickers = by_er(set(current) - set(earlier), current)[:_DELTA_ROWS_SHOWN]
    exited_tickers = by_er(set(earlier) - set(current), earlier)[:_DELTA_ROWS_SHOWN]
    # A name that reported between the two runs entered on new numbers, not on a price
    # move alone. The review_set output carries no disclosure date, so it is read from
    # the market store for the tickers that actually changed side. When that store
    # cannot answer, the fact stays unknown instead of reading as "no disclosure".
    disclosed: set[str] | None = None
    if market is not None:
        disclosed = set(
            market.disclosures_after(entered_tickers + exited_tickers, after=previous_asof)
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
    moves: list[CandidateMoveDeltaView] = []
    for ticker in sorted(set(current) & set(earlier)):
        current_er = _percent(_pool_er(current[ticker]))
        previous_er = _percent(_pool_er(earlier[ticker]))
        if current_er is None or previous_er is None:
            continue
        change = round(current_er - previous_er, 1)
        if abs(change) < _DELTA_ER_MOVE_MIN_PP:
            continue
        moves.append(
            CandidateMoveDeltaView(
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
    asof: date,
    previous_asof: date | None,
) -> tuple[list[HoldingDeltaView], int, int]:
    tickers = [holding.ticker for holding in snapshot.holdings]
    if not tickers:
        return [], 0, 0
    latest_by_ticker = market.latest_closes(tickers)
    # The change is asked of the market layer so both ends land on the same share
    # basis; dividing two stored closes would report a split as a price move.
    changes = (
        {} if previous_asof is None else market.close_changes_since(tickers, since=previous_asof)
    )
    earnings = market.next_earnings_dates(tickers, asof=asof)
    revisions = _latest_research_by_ticker(research.revisions())
    rows: list[HoldingDeltaView] = []
    without_fair_value = 0
    without_price = 0
    for holding in snapshot.holdings:
        revision = revisions.get(holding.ticker)
        fair_value = None if revision is None else revision.current_fair_value_yen
        close = latest_by_ticker.get(holding.ticker)
        close_value = None if close is None else close[0]
        if fair_value is None:
            without_fair_value += 1
        elif close_value is None:
            # A holding with a fair value the store cannot price is neither compared
            # nor absent: counting it keeps the silence from reading as "not reached".
            without_price += 1
        change = changes.get(holding.ticker)
        at_or_above = (
            None if fair_value is None or close_value is None else bool(close_value >= fair_value)
        )
        moved = change is not None and abs(change) >= _DELTA_HOLDING_MOVE_MIN_PCT
        if at_or_above is not True and not moved:
            continue
        next_earnings = earnings.get(holding.ticker)
        rows.append(
            HoldingDeltaView(
                ticker=holding.ticker,
                company_name=None if revision is None else revision.company_name,
                at_or_above_fair_value=at_or_above,
                change_since_previous_pct=change,
                days_to_next_earnings=(
                    None if next_earnings is None else (next_earnings - asof).days
                ),
            )
        )
    return rows, without_fair_value, without_price


def _reading_by_series(payload: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    series = payload.get("series")
    if not isinstance(series, list):
        return {}
    return {
        str(item.get("series_id")): item
        for item in series
        if isinstance(item, Mapping) and item.get("series_id")
    }


def _flag_set(reading: Mapping[str, object] | None) -> set[str]:
    if reading is None:
        return set()
    raw = reading.get("flags")
    return {str(item) for item in raw} if isinstance(raw, list) else set()


def _percent(value: float | None) -> float | None:
    """Machine E[r] is stored as an annual ratio; the view reports percent."""
    return None if value is None else round(value * 100, 1)
