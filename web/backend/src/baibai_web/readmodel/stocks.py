"""Show screening, assessment, and security state through stock read models."""

from __future__ import annotations

from collections.abc import (
    Mapping,
)
from dataclasses import dataclass
from datetime import (
    date,
    datetime,
    timedelta,
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
    DbScreeningSource,
)
from baibai_web.sources.protocols import (
    LedgerSource,
    MarketPriceSource,
    ResearchSource,
    ScreeningSource,
)
from baibai_web.sources.types import (
    ErLevelCalibrationContext,
    ResearchRevision,
    ScreeningRunRecord,
    ThesisDetail,
)

from .builders import _holding_view, _number, _text
from .models import (
    AllocationAlternativeView,
    AssessmentReviewView,
    CapitalAllocationAssessmentSummaryView,
    CapitalAllocationAssessmentView,
    ErLevelCalibrationBandView,
    ErLevelCalibrationBasisView,
    ErLevelCalibrationContextView,
    ErLevelCalibrationHorizonView,
    ErLevelCalibrationStatsView,
    FvConvergenceView,
    PortfolioState,
    PositionReviewView,
    ResearchRevisionView,
    ResearchTriageEntryView,
    ResearchTriageView,
    ReviewSetAnalysisView,
    ReviewSetEntrySnapshotView,
    ReviewSetEntryView,
    ReviewSetNominationView,
    ReviewSetView,
    ScreeningHistoryRunView,
    ScreeningRunView,
    ScreeningView,
    SecurityAnalysisRowView,
    SecurityDetailView,
    ThesisDetailView,
)

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


_METRIC_TEXT_FIELDS = ("dividend_basis",)


_STALE_RUN_AGE = timedelta(days=7)


def build_screening(
    screening: ScreeningSource,
    ledger: LedgerSource,
    research: ResearchSource,
    er_level_calibration: ErLevelCalibrationContext | None = None,
) -> ScreeningView:
    """Build the latest Security Analysis view with portfolio/research annotations."""

    run, review_sets = _operative_run(screening)
    applicable_calibration = _calibration_for_run(run, er_level_calibration)
    research_triages: list[ResearchTriageView] = []
    assessments: list[CapitalAllocationAssessmentSummaryView] = []
    research_triages, assessments = _current_screening_judgments(
        research,
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
    fair_value = _fair_value_by_ticker(review_sets)
    today = datetime.now(_JST).date()
    return ScreeningView(
        run=_screening_run_view(run, today=today),
        security_analyses=[
            _security_analysis_row_view(
                row,
                held=held,
                reserved=reserved,
                researched=researched,
                fair_value=fair_value,
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
    research: ResearchSource,
    *,
    review_sets: list[ReviewSetView],
) -> tuple[list[ResearchTriageView], list[CapitalAllocationAssessmentSummaryView]]:
    """Show only the Research Triage -> allocation judgments for the displayed Review Set."""

    review_set_sources = {
        (review_set.review_set_id, review_set.run_revision_id) for review_set in review_sets
    }
    current_triages = [
        item
        for item in research.research_triages(
            review_set_ids=[review_set.review_set_id for review_set in review_sets]
        )
        if (str(item.get("review_set_id")), str(item.get("run_revision_id"))) in review_set_sources
    ]
    triage_ids = {str(item["research_triage_id"]) for item in current_triages}
    triage_views = [_research_triage_view(item) for item in current_triages]
    if not triage_ids:
        return triage_views, []
    current_assessments = [
        item for item in research.assessments() if str(item.get("research_triage_id")) in triage_ids
    ]
    return (
        triage_views,
        [_assessment_summary_view(item) for item in current_assessments],
    )


def _calibration_for_run(
    run: ScreeningRunRecord | None,
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
    screening: ScreeningSource,
) -> tuple[ScreeningRunRecord | None, list[ReviewSetView]]:
    """Resolve the run every screening surface reads, with its own review_sets.

    The L2 machine Review Set binds to a specific run revision; the L3 Research
    Triage refers to that published set.
    A newer revision of the same as-of — a determinism re-run, say — carries no
    review_set of its own, so presenting it would blank the machine-review_set view
    and the FV anchors that hang off it. Falling back to the newest review_set's run
    keeps Security Analyses, their review_sets, and the research_triage join on one run.

    Every surface that shows a candidate resolves the run here, so the list and the
    security page cannot end up describing the same ticker from different runs.
    """

    run = screening.latest_run()
    if run is None:
        return None, []
    # Ask for this run's review_sets first: the whole published history is only needed to
    # find a fallback, which is the uncommon case, and a security page reads this on
    # every request.
    run_review_sets = screening.review_sets(run_revision_id=run.run_revision_id)
    if not run_review_sets and (all_review_sets := screening.review_sets()):
        newest = max(all_review_sets, key=lambda item: str(item["created_at"]))
        fallback_run = screening.run(str(newest["run_revision_id"]))
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
    return entry.analysis.expected_return.fv_sector_median_yen


def build_screening_history_run(
    screening: DbScreeningSource,
    ledger: LedgerSource,
    research: ResearchSource,
    *,
    as_of: date,
) -> ScreeningHistoryRunView | None:
    """Build one retained run with the same current-state annotations as latest."""

    run = screening.run_as_of(as_of)
    if run is None:
        return None
    held, reserved = _held_and_reserved_tickers(ledger)
    researched = {item.ticker for item in research.revisions()}
    fair_value = _fair_value_by_ticker(
        [
            _machine_review_set_view(item)
            for item in screening.review_sets(run_revision_id=run.run_revision_id)
        ]
    )
    return ScreeningHistoryRunView(
        run=_screening_run_view(run, today=datetime.now(_JST).date()),
        rows=[
            _security_analysis_row_view(
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
    research: ResearchSource,
    *,
    capital_allocation_assessment_id: str,
) -> CapitalAllocationAssessmentView | None:
    """Build one published capital-allocation assessment for the detail page."""

    raw = research.assessment(capital_allocation_assessment_id)
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
        decision=str(raw["result"]),
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
        decision=str(raw["result"]),
        headline=str(raw["headline"]),
        research_triage_id=str(raw["research_triage_id"]),
        macro_context_id=_text(raw.get("macro_context_id")),
        comparison=str(raw["comparison"]),
        foregone_alternatives=str(raw["forgone"]),
        alternatives=[
            _allocation_alternative_view(item)
            for item in _mapping_items_optional(raw.get("alternatives"))
        ],
        content_review=AssessmentReviewView.model_validate(raw["review"]),
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
        case_status=_text(_mapping_optional(machine_values.get("investment_case")).get("status")),
        base_annualized_return_pct=_number(
            _mapping_optional(_mapping_optional(machine_values.get("projections")).get("base")).get(
                "annualized_return_pct"
            )
        ),
        pmax_raw_yen=_number(machine_values.get("pmax_raw_yen")),
        valuation_as_of=_text(machine_values.get("as_of")),
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
    context = analysis.get("context")
    if not isinstance(context, Mapping):
        raise ValueError("Review Set entry context must be an object")
    normalized_context = {
        **context,
        "large_holding_filing_within_lookback": context.get("large_holding_event_recent"),
        "tender_offer_filing_within_lookback": context.get("tender_offer_event_recent"),
    }
    normalized_analysis = {**analysis, "context": normalized_context}
    return ReviewSetEntryView(
        ticker=str(raw.get("ticker", "")),
        name=_text(raw.get("name")),
        sector_33=_text(raw.get("sector_33")),
        nominations=[ReviewSetNominationView.model_validate(item) for item in nominations],
        analysis=ReviewSetAnalysisView.model_validate(normalized_analysis),
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


def _research_triage_entry_view(raw: Mapping[str, object]) -> ResearchTriageEntryView:
    """Project one current Research Triage entry and its judgment-time snapshot."""

    snapshot = raw.get("candidate_snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("Research Triage candidate_snapshot must be an object")
    analysis = snapshot.get("analysis")
    if not isinstance(analysis, Mapping):
        raise ValueError("Research Triage Review Set Entry analysis must be an object")
    normalized = {
        "name": snapshot.get("name"),
        "sector_33": snapshot.get("sector_33"),
        "nominations": snapshot.get("nominations"),
        "expected_return": analysis.get("expected_return"),
        "data_quality": analysis.get("data_quality"),
    }
    return ResearchTriageEntryView.model_validate(
        {**raw, "review_set_entry_snapshot": ReviewSetEntrySnapshotView.model_validate(normalized)}
    )


def _research_triage_view(raw: Mapping[str, object]) -> ResearchTriageView:
    entries = [_research_triage_entry_view(item) for item in _mapping_items(raw.get("entries"))]
    return ResearchTriageView(
        research_triage_id=str(raw["research_triage_id"]),
        review_set_id=str(raw["review_set_id"]),
        run_revision_id=str(raw["run_revision_id"]),
        as_of=date.fromisoformat(str(raw["as_of"])),
        published_at=datetime.fromisoformat(str(raw["published_at"])),
        entries=entries,
    )


def _mapping_items_optional(value: object) -> list[Mapping[str, object]]:
    return [] if value is None else _mapping_items(value)


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
        _text(raw_security_analysis.get("name")) if raw_security_analysis is not None else None
    )
    security_sector = (
        _text(raw_security_analysis.get("sector_33")) if raw_security_analysis is not None else None
    )
    holding = (
        _holding_view(
            holding_snapshot,
            revision=latest_revision,
            security_name=security_name,
            market_close=market.latest_closes([ticker]).get(ticker),
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


def _mapping_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError("expected an array of objects")
    return [item for item in value if isinstance(item, Mapping)]


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
    if metrics.get("stale_fin_flag") is True:
        flags.append("stale_fin")
    dividend_basis = metrics.get("dividend_basis")
    if isinstance(dividend_basis, str) and (
        dividend_basis.startswith("unresolved_") or dividend_basis == "unavailable"
    ):
        flags.append(dividend_basis)
    return flags


def _screening_run_view(run: ScreeningRunRecord, *, today: date) -> ScreeningRunView:
    return ScreeningRunView(
        run_id=run.run_id,
        as_of=run.as_of,
        generated_at=run.generated_at,
        universe_size=run.universe_size,
        analyzed_security_count=len(run.rows),
        run_revision_id=run.run_revision_id,
        stale=run.as_of <= today - _STALE_RUN_AGE,
    )


def _security_analysis_row_view(
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
    # unresolved_split_basis を無配と読み違えないために Security Analysis 一覧まで届ける必要がある。
    text_values = {name: _text(metrics.get(name)) for name in _METRIC_TEXT_FIELDS}
    flags = _data_quality_flags(row, metrics)
    review_entry = (fair_value or {}).get(ticker)
    anchor = _review_entry_fair_value(review_entry)
    market_price = _number(metrics.get("market_price_yen"))
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
        fair_value_gap_pct=_fair_value_gap_pct(anchor, market_price),
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
                as_of_start=item.as_of_start,
                as_of_end=item.as_of_end,
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
        disposition=revision.disposition,
        pmax_raw_yen=revision.pmax_raw_yen,
        review_id=revision.review_id,
        status=revision.status,
    )


def _thesis_detail_view(detail: ThesisDetail) -> ThesisDetailView:
    return ThesisDetailView(
        revision=_research_revision_view(detail.revision), projection=detail.projection
    )


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _mapping_optional(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
