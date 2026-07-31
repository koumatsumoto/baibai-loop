from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from baibai_app.readmodel.builders import (
    _candidate_row_view,
    _fair_value_by_ticker,
    _machine_selection_view,
    _shortlist_view,
    build_daily_delta,
    build_dashboard,
    build_screening,
    build_security_detail,
)
from baibai_app.sources.types import (
    CandidatesRun,
    ResearchRevision,
    ScenarioSummary,
    TaskRecord,
    ThesisDetail,
)
from baibai_engine.position.ledger import (
    HoldingSnapshot,
    PortfolioLedgerError,
    PortfolioSnapshot,
    ReservationSnapshot,
)

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 7, 18, 12, 0, tzinfo=JST)


class StubLedger:
    def __init__(self, snapshot: PortfolioSnapshot | None, error: str | None = None):
        self._snapshot = snapshot
        self._error = error

    def exists(self):
        return self._snapshot is not None or self._error is not None

    def snapshot(self):
        if self._error is not None:
            raise PortfolioLedgerError(self._error)
        assert self._snapshot is not None
        return self._snapshot


class StubResearch:
    def __init__(self, revisions: list[ResearchRevision]):
        self._revisions = revisions

    def revisions(self):
        return list(self._revisions)

    def thesis_detail(self, thesis_id: str):
        revision = next(item for item in self._revisions if item.thesis_id == thesis_id)
        return ThesisDetail(
            revision=revision,
            entry_price_basis_yen=10.0,
            required_5y_base_cagr_pct=8.0,
            permanent_loss_risk_count=7,
            scenarios=(ScenarioSummary(name="base", horizon_years=5),),
            permanent_loss_conclusion="acceptable",
            strongest_countercase="需要減速",
            sizing_action="normal",
        )

    def load_errors(self):
        return []

    def holding_reviews(self, *, ticker: str | None = None):
        return []


class StubTasks:
    def __init__(self, records: list[TaskRecord], *, exists: bool = True):
        self._records = records
        self._exists = exists

    def exists(self):
        return self._exists

    def list_tasks(self):
        return list(self._records)


class StubCandidates:
    def __init__(
        self, run: CandidatesRun | None, selections: list[dict[str, object]] | None = None
    ):
        self._run = run
        self._selections = selections or []

    def latest_run(self):
        return self._run

    def run(self, run_revision_id: str):
        if self._run is not None and self._run.run_revision_id == run_revision_id:
            return self._run
        return None

    def selections(self, *, run_revision_id: str | None = None):
        return list(self._selections)


class StubFallbackCandidates:
    """Latest run holds no selection, so both surfaces must resolve to `fallback`."""

    def __init__(
        self,
        *,
        latest: CandidatesRun,
        fallback: CandidatesRun,
        selections: list[dict[str, object]],
    ):
        self._latest = latest
        self._fallback = fallback
        self._selections = selections

    def latest_run(self):
        return self._latest

    def run(self, run_revision_id: str):
        return self._fallback if run_revision_id == self._fallback.run_revision_id else None

    def selections(self, *, run_revision_id: str | None = None):
        if run_revision_id is None:
            return list(self._selections)
        return [item for item in self._selections if item["run_revision_id"] == run_revision_id]


class StubMarket:
    def __init__(
        self,
        closes: dict[str, tuple[float, date]] | None = None,
        earnings: dict[str, date] | None = None,
    ):
        self._closes = closes or {}
        self._earnings = earnings or {}

    def latest_closes(self, tickers):
        return {ticker: self._closes[ticker] for ticker in tickers if ticker in self._closes}

    def next_earnings_dates(self, tickers, *, asof):
        return {
            ticker: self._earnings[ticker]
            for ticker in tickers
            if ticker in self._earnings and self._earnings[ticker] >= asof
        }


def _snapshot() -> PortfolioSnapshot:
    holding = HoldingSnapshot(
        ticker="4432",
        sector="情報・通信業",
        common_factors=(),
        quantity=100,
        deployed_cost_yen=800,
        market_price_yen=Decimal("10"),
        market_price_observed_at=NOW,
        market_price_source_kind="test_fixture",
        market_price_basis="unadjusted_close",
        market_price_source_ref="fixture",
        market_value_yen=1000,
    )
    return PortfolioSnapshot(
        as_of=NOW,
        portfolio_scope="repository_only",
        available_cash_yen=1000,
        reserved_cash_yen=0,
        deployed_cost_yen=800,
        holdings_market_value_yen=1000,
        confirmed_income_yen=0,
        confirmed_cost_yen=0,
        confirmed_tax_yen=0,
        confirmed_cost_tax_yen=0,
        book_capital_yen=1800,
        total_capital_yen=2000,
        estimated_exit_tax_rate_bps=None,
        estimated_exit_tax_basis=None,
        estimated_exit_tax_yen=None,
        holdings=(holding,),
        active_reservations=(),
        warnings=(),
    )


def _revision() -> ResearchRevision:
    return ResearchRevision(
        ticker="4432",
        company_name="ウイングアーク１ｓｔ",
        sector="情報・通信業",
        as_of=date(2026, 7, 14),
        thesis_id="thesis-20260714-4432-r1",
        recommendation="buy",
        confidence="medium",
        current_fair_value_yen=12.0,
        model_version="thesis-v2",
        review_id=None,
    )


def _run(*, metrics: object = None) -> CandidatesRun:
    row: dict[str, object] = {
        "ticker": "4432",
        "name": "ウイングアーク１ｓｔ",
        "sector_33": "情報・通信業",
        "per_trailing": "invalid",
    }
    if metrics is not None:
        row["metrics"] = metrics
    return CandidatesRun(
        run_id="screening-20260708",
        run_date=date(2026, 7, 8),
        asof_date=date(2026, 7, 8),
        run_at=datetime(2026, 7, 8, 12, 0, tzinfo=JST),
        universe_size=3744,
        run_revision_id="run-revision-20260708",
        rules_ref=None,
        rows=(row,),
    )


def test_dashboard_calculates_unrealized_pnl_and_fv_gap_with_expected_sign() -> None:
    view = build_dashboard(
        StubLedger(_snapshot()),
        StubResearch([_revision()]),
        StubTasks([]),
        StubCandidates(_run()),
        StubMarket(),
    )

    holding = view.holdings[0]
    assert holding.unrealized_pnl_yen == 200
    assert holding.unrealized_pnl_pct == 25.0
    assert holding.fair_value_yen == 12.0
    assert holding.fv_gap_pct == 20.0
    assert holding.market_price_yen == "10"


def test_dashboard_keeps_fv_fields_empty_without_thesis() -> None:
    view = build_dashboard(
        StubLedger(_snapshot()),
        StubResearch([]),
        StubTasks([]),
        StubCandidates(_run()),
        StubMarket(),
    )

    holding = view.holdings[0]
    assert holding.company_name == "ウイングアーク１ｓｔ"
    assert holding.fair_value_yen is None
    assert holding.fv_gap_pct is None
    assert holding.latest_thesis_id is None


def test_dashboard_returns_task_data_when_ledger_is_absent_or_invalid() -> None:
    tasks = StubTasks(
        [
            TaskRecord(
                task_id="task-20000101-overdue",
                title="期限超過task",
                kind="ops",
                status="open",
                ticker=None,
                due_date=date(2000, 1, 1),
                event_label=None,
                event_date=None,
                body_md=None,
                related_refs=(),
                created_at=date(1999, 1, 1),
                closed_at=None,
            ),
            TaskRecord(
                task_id="task-29990102-event",
                title="将来event",
                kind="earnings-review",
                status="open",
                ticker="4432",
                due_date=date(2999, 1, 2),
                event_label="決算",
                event_date=date(2999, 1, 1),
                body_md=None,
                related_refs=(),
                created_at=date(2026, 1, 1),
                closed_at=None,
            ),
        ]
    )
    absent = build_dashboard(
        StubLedger(None), StubResearch([]), tasks, StubCandidates(None), StubMarket()
    )
    invalid = build_dashboard(
        StubLedger(None, error="missing market price"),
        StubResearch([]),
        tasks,
        StubCandidates(None),
        StubMarket(),
    )

    assert absent.ledger_exists is False
    assert absent.next_task is not None
    assert absent.next_task.task_id == "task-20000101-overdue"
    assert absent.next_task.overdue is True
    # The task carrying an event date is still just a task: it takes its place in the
    # open list by due date and gets no separate slot of its own.
    assert [item.task_id for item in absent.open_tasks] == [
        "task-20000101-overdue",
        "task-29990102-event",
    ]
    assert invalid.ledger_exists is True
    assert invalid.ledger_error == "missing market price"
    assert invalid.total_capital_yen is None


def test_screening_tolerates_missing_or_invalid_metrics() -> None:
    view = build_screening(
        StubCandidates(_run()),
        StubLedger(_snapshot()),
        StubResearch([_revision()]),
    )

    assert view.run is not None
    assert view.run.candidate_count == 1
    assert view.run.run_at == datetime(2026, 7, 8, 12, 0, tzinfo=JST)
    row = view.rows[0]
    assert row.per_trailing is None
    assert row.er_annual is None
    assert row.portfolio_state == "held"
    assert row.has_research is True


def test_security_detail_is_none_only_when_all_sources_are_empty() -> None:
    assert (
        build_security_detail(
            "0000",
            StubLedger(None),
            StubResearch([]),
            StubCandidates(None),
            StubMarket(),
        )
        is None
    )
    detail = build_security_detail(
        "4432",
        StubLedger(_snapshot()),
        StubResearch([_revision()]),
        StubCandidates(_run(metrics={"er_annual": 0.1})),
        StubMarket(),
    )
    assert detail is not None
    assert detail.latest_thesis is not None
    assert detail.latest_thesis.permanent_loss_risk_count == 7
    assert detail.candidate_row is not None
    assert detail.candidate_row.er_annual == 0.1


def _candidate_row(row: dict[str, object]):
    return _candidate_row_view(row, held=set(), reserved=set(), researched=set())


def test_data_quality_flags_include_forecast_special_gain() -> None:
    view = _candidate_row({"ticker": "4849", "metrics": {"forecast_special_gain_flag": True}})

    assert "一時益予想" in view.data_quality_flags


def test_data_quality_flags_omit_forecast_special_gain_when_false() -> None:
    view = _candidate_row({"ticker": "4849", "metrics": {"forecast_special_gain_flag": False}})

    assert "一時益予想" not in view.data_quality_flags


def test_supply_demand_flags_raise_on_a_crowded_margin_long() -> None:
    view = _candidate_row({"ticker": "4849", "metrics": {"margin_long_to_adv": 6.0}})

    assert "信用買い混雑" in view.data_quality_flags


def test_supply_demand_flags_stay_quiet_below_the_measured_decile() -> None:
    view = _candidate_row({"ticker": "4849", "metrics": {"margin_long_to_adv": 1.4}})

    assert "信用買い混雑" not in view.data_quality_flags


def test_supply_demand_flags_raise_on_a_deadline_heavy_long_balance() -> None:
    view = _candidate_row({"ticker": "4849", "metrics": {"margin_std_long_share": 0.9}})

    assert "制度期日偏重" in view.data_quality_flags


def test_a_rejected_axis_is_shown_as_a_number_and_never_raises_a_flag() -> None:
    # `margin_long_delta_26w` and `margin_long_share` failed the acceptance
    # criteria, so they are context for the reader rather than a warning. A flag
    # built on them would give an untested rule the weight of a tested one.
    view = _candidate_row(
        {
            "ticker": "4849",
            "metrics": {"margin_long_delta_26w": -0.9, "margin_long_share": 1.0},
        }
    )

    assert view.data_quality_flags == []


def test_an_unobserved_margin_balance_raises_nothing() -> None:
    view = _candidate_row({"ticker": "4849", "metrics": {}})

    assert view.data_quality_flags == []


def test_holding_uses_market_close_when_strictly_newer_than_ledger() -> None:
    view = build_dashboard(
        StubLedger(_snapshot()),
        StubResearch([_revision()]),
        StubTasks([]),
        StubCandidates(_run()),
        StubMarket({"4432": (11.0, date(2026, 7, 19))}),
    )

    holding = view.holdings[0]
    assert holding.market_price_yen == "11"
    assert holding.market_price_as_of == datetime(2026, 7, 19, 15, 30, tzinfo=JST)
    assert holding.market_value_yen == 1100
    assert holding.unrealized_pnl_yen == 300
    assert holding.unrealized_pnl_pct == 37.5
    assert holding.fv_gap_pct == 9.1
    assert view.holdings_market_value_yen == 1100
    assert view.total_capital_yen == 2100
    assert view.valuation_as_of == datetime(2026, 7, 19, 15, 30, tzinfo=JST)


def test_dashboard_valuation_basis_uses_oldest_close_when_holding_dates_differ() -> None:
    snapshot = _snapshot()
    ledger_as_of = datetime(2026, 7, 15, 9, 0, tzinfo=JST)
    first = replace(snapshot.holdings[0], market_price_observed_at=ledger_as_of)
    second = replace(first, ticker="9999")
    mixed = replace(snapshot, as_of=ledger_as_of, holdings=(first, second))

    view = build_dashboard(
        StubLedger(mixed),
        StubResearch([_revision()]),
        StubTasks([]),
        StubCandidates(_run()),
        StubMarket(
            {
                "4432": (11.0, date(2026, 7, 19)),
                "9999": (9.0, date(2026, 7, 16)),
            }
        ),
    )

    assert view.valuation_as_of == datetime(2026, 7, 16, 15, 30, tzinfo=JST)


def _snapshot_with_reservation(*, expires_at: datetime) -> PortfolioSnapshot:
    base = _snapshot()
    reservation = ReservationSnapshot(
        reservation_id="reservation-8929-pending",
        order_id="order-8929",
        ticker="8929",
        sector="不動産業",
        common_factors=(),
        decision_reference=None,
        remaining_quantity=100,
        price_guard_yen=Decimal("1200"),
        reserved_yen=120_000,
        expires_at=expires_at,
    )
    return PortfolioSnapshot(
        **{
            field: getattr(base, field)
            for field in base.__dataclass_fields__
            if field != "active_reservations"
        },
        active_reservations=(reservation,),
    )


def test_holding_carries_next_earnings_date_from_market_source() -> None:
    today = datetime.now(JST).date()
    earnings_date = today + timedelta(days=3)
    view = build_dashboard(
        StubLedger(_snapshot()),
        StubResearch([_revision()]),
        StubTasks([]),
        StubCandidates(_run()),
        StubMarket(earnings={"4432": earnings_date}),
    )

    assert view.holdings[0].next_earnings_date == earnings_date.isoformat()


def test_upcoming_events_merges_earnings_and_reservation_within_window_in_order() -> None:
    today = datetime.now(JST).date()
    earnings_date = today + timedelta(days=10)
    reservation_expiry = today + timedelta(days=5)
    view = build_dashboard(
        StubLedger(
            _snapshot_with_reservation(
                expires_at=datetime.combine(reservation_expiry, time(15, 30), tzinfo=JST)
            )
        ),
        StubResearch([_revision()]),
        StubTasks([]),
        StubCandidates(_run()),
        StubMarket(earnings={"4432": earnings_date}),
    )

    events = view.upcoming_events
    assert [(item.kind, item.days_until) for item in events] == [
        ("reservation_expiry", 5),
        ("earnings", 10),
    ]
    assert events[0].ticker == "8929"
    assert events[1].ticker == "4432"
    assert events[1].label == "ウイングアーク１ｓｔ"


def test_upcoming_events_excludes_dates_outside_the_fourteen_day_window() -> None:
    today = datetime.now(JST).date()
    view = build_dashboard(
        StubLedger(_snapshot()),
        StubResearch([_revision()]),
        StubTasks([]),
        StubCandidates(_run()),
        StubMarket(earnings={"4432": today + timedelta(days=30)}),
    )

    assert view.upcoming_events == []


def test_holding_keeps_ledger_price_when_market_close_is_not_newer() -> None:
    view = build_dashboard(
        StubLedger(_snapshot()),
        StubResearch([_revision()]),
        StubTasks([]),
        StubCandidates(_run()),
        StubMarket({"4432": (999.0, date(2026, 7, 18))}),
    )

    holding = view.holdings[0]
    assert holding.market_price_yen == "10"
    assert holding.market_price_as_of == NOW
    assert holding.market_value_yen == 1000
    assert view.holdings_market_value_yen == 1000
    assert view.total_capital_yen == 2000


def _v2_narrative() -> dict[str, str]:
    """RR block と rank を持たない、schema v3 以前の発行済み entry。"""
    return {
        "ploss": "中低",
        "why": "受注端境",
        "temporary": "翌期に戻る",
        "structural": "毀損はない",
        "survive": "net cashで耐える",
        "unlock": "還元強化",
        "counter": "構造鈍化",
        "research": "受注残を確認",
        "value": "FV乖離が大きい",
        "prov": "深掘り",
    }


def test_shortlist_view_keeps_reading_entries_published_before_the_risk_reward_block() -> None:
    view = _shortlist_view(
        {
            "shortlist_id": "shortlist-20260717-legacy",
            "selection_id": "selection-legacy",
            "run_revision_id": "runrev-legacy",
            "as_of": "2026-07-17",
            "published_at": "2026-07-17T15:00:00+09:00",
            "entries": [
                {
                    "ticker": "2331",
                    "decision": "selected",
                    "reason": "深掘りへ",
                    "narrative": _v2_narrative(),
                }
            ],
        }
    )

    assert view.unreadable_entries == 0
    entry = view.entries[0]
    assert entry.rank is None
    assert entry.narrative is not None
    assert entry.narrative.why == "受注端境"
    assert entry.narrative.upside is None
    assert entry.narrative.catalyst_date is None


def test_shortlist_view_counts_unreadable_entries_instead_of_dropping_the_surface() -> None:
    view = _shortlist_view(
        {
            "shortlist_id": "shortlist-20260717-mixed",
            "selection_id": "selection-mixed",
            "run_revision_id": "runrev-mixed",
            "as_of": "2026-07-17",
            "published_at": "2026-07-17T15:00:00+09:00",
            "entries": [
                {"decision": "selected", "reason": "ticker が無い"},
                {"ticker": "0001", "decision": "rejected", "reason": "根拠が弱い"},
            ],
        }
    )

    assert [entry.ticker for entry in view.entries] == ["0001"]
    assert view.unreadable_entries == 1


def test_fair_value_reaches_only_longlist_members_and_uses_the_newest_selection() -> None:
    older = _machine_selection_view(
        {
            "selection_id": "selection-old",
            "run_revision_id": "runrev-1",
            "profile": "value",
            "macro_context_id": None,
            "created_at": "2026-07-21T02:00:00+09:00",
            "payload": {"longlist": [{"ticker": "4432", "fair_value_anchor_yen": 5.0}]},
        }
    )
    newer = _machine_selection_view(
        {
            "selection_id": "selection-new",
            "run_revision_id": "runrev-1",
            "profile": "value",
            "macro_context_id": None,
            "created_at": "2026-07-21T13:00:00+09:00",
            "payload": {
                "longlist": [
                    {
                        "ticker": "4432",
                        "rank": 1,
                        "market_price_yen": 10.0,
                        "fair_value_anchor_yen": 12.5,
                        "expected_return_pct": 10.82,
                        "event_warnings": ["earnings_scheduled"],
                    }
                ]
            },
        }
    )
    fair_value = _fair_value_by_ticker([older, newer])

    assert newer.longlist[0].fair_value_gap_pct == 25.0
    assert newer.longlist[0].event_warnings == ["earnings_scheduled"]

    member = _candidate_row_view(
        {"ticker": "4432"}, held=set(), reserved=set(), researched=set(), fair_value=fair_value
    )
    outsider = _candidate_row_view(
        {"ticker": "0001"}, held=set(), reserved=set(), researched=set(), fair_value=fair_value
    )

    assert member.fair_value_anchor_yen == 12.5
    assert member.fair_value_gap_pct == 25.0
    assert outsider.fair_value_anchor_yen is None
    assert outsider.fair_value_gap_pct is None


def test_security_detail_shows_the_same_fv_anchor_as_the_stocks_list() -> None:
    selections: list[dict[str, object]] = [
        {
            "selection_id": "selection-1",
            "run_revision_id": "run-revision-20260708",
            "profile": "value",
            "macro_context_id": None,
            "created_at": "2026-07-21T13:00:00+09:00",
            "payload": {
                "longlist": [
                    {"ticker": "4432", "market_price_yen": 10.0, "fair_value_anchor_yen": 12.5}
                ]
            },
        }
    ]
    candidates = StubCandidates(_run(), selections)
    detail = build_security_detail(
        "4432", StubLedger(None), StubResearch([]), candidates, StubMarket()
    )
    # The list builds its row from the same run's selections, so pinning the shared
    # helper here is what keeps the two surfaces from disagreeing about one ticker.
    listed_row = _candidate_row_view(
        _run().rows[0],
        held=set(),
        reserved=set(),
        researched=set(),
        fair_value=_fair_value_by_ticker([_machine_selection_view(selections[0])]),
    )

    assert detail is not None
    assert detail.candidate_row is not None
    assert detail.candidate_row.fair_value_anchor_yen == 12.5
    assert detail.candidate_row.fair_value_gap_pct == listed_row.fair_value_gap_pct


def test_both_screening_surfaces_fall_back_to_the_same_run() -> None:
    """A determinism re-run carries no selection; both surfaces must follow the fallback."""

    selections: list[dict[str, object]] = [
        {
            "selection_id": "selection-1",
            "run_revision_id": "run-revision-with-selection",
            "profile": "value",
            "macro_context_id": None,
            "created_at": "2026-07-21T13:00:00+09:00",
            "payload": {
                "longlist": [
                    {"ticker": "4432", "market_price_yen": 10.0, "fair_value_anchor_yen": 12.5}
                ]
            },
        }
    ]
    rerun = replace(_run(), run_revision_id="run-revision-rerun-without-selection")
    selected_run = replace(_run(), run_revision_id="run-revision-with-selection")
    candidates = StubFallbackCandidates(latest=rerun, fallback=selected_run, selections=selections)

    listed = build_screening(candidates, StubLedger(None), StubResearch([]))
    detail = build_security_detail(
        "4432", StubLedger(None), StubResearch([]), candidates, StubMarket()
    )

    assert detail is not None
    assert detail.candidate_row is not None
    assert listed.run is not None
    assert detail.candidate_run is not None
    assert detail.candidate_run.run_revision_id == listed.run.run_revision_id
    assert detail.candidate_row.fair_value_anchor_yen == 12.5


class StubDeltaCandidates:
    """Two runs with their machine selections, so the delta has a pool to compare."""

    def __init__(
        self,
        latest: CandidatesRun | None,
        previous: CandidatesRun | None,
        pools: dict[str, dict[str, list[dict[str, object]]]] | None = None,
    ) -> None:
        self._latest = latest
        self._previous = previous
        self._pools = pools or {}

    def latest_run(self):
        return self._latest

    def previous_run(self):
        return self._previous

    def run(self, run_revision_id: str):
        for run in (self._latest, self._previous):
            if run is not None and run.run_revision_id == run_revision_id:
                return run
        return None

    def selections(self, *, run_revision_id: str | None = None):
        payload = self._pools.get(run_revision_id or "")
        if payload is None:
            return []
        return [
            {
                "selection_id": f"selection-{run_revision_id}",
                "run_revision_id": run_revision_id,
                "publication_kind": "machine",
                "payload": payload,
            }
        ]


class StubDeltaMarket(StubMarket):
    """Records the dates it is asked for, so a caller passing the wrong one fails."""

    def __init__(
        self,
        closes: dict[str, tuple[float, date]] | None = None,
        earnings: dict[str, date] | None = None,
        changes: dict[str, float] | None = None,
        disclosures: dict[str, date] | None = None,
        previous_business: date | None = date(2026, 7, 28),
        exists: bool = True,
    ) -> None:
        super().__init__(closes, earnings)
        self._changes = changes or {}
        self._disclosures = disclosures or {}
        self._previous_business = previous_business
        self._exists = exists
        self.change_sinces: list[date] = []
        self.disclosure_afters: list[date] = []
        self.business_day_queries: list[date] = []

    def exists(self) -> bool:
        return self._exists

    def previous_business_day(self, day: date) -> date | None:
        self.business_day_queries.append(day)
        return self._previous_business

    def close_changes_since(self, tickers, *, since):
        self.change_sinces.append(since)
        return {ticker: self._changes[ticker] for ticker in tickers if ticker in self._changes}

    def disclosures_after(self, tickers, *, after):
        self.disclosure_afters.append(after)
        return {
            ticker: self._disclosures[ticker] for ticker in tickers if ticker in self._disclosures
        }


class StubDeltaMacro:
    def __init__(self, readings: dict[date, dict[str, object] | None]) -> None:
        self._readings = readings

    def reading(self, *, asof: date):
        return self._readings.get(asof)


def _delta_run(*, asof: date, revision: str, rules_ref: str | None = "rules-a") -> CandidatesRun:
    return CandidatesRun(
        run_id=f"screening-{asof.isoformat()}",
        run_date=asof,
        asof_date=asof,
        run_at=datetime(asof.year, asof.month, asof.day, 18, 30, tzinfo=JST),
        universe_size=3744,
        run_revision_id=revision,
        rules_ref=rules_ref,
        rows=(),
    )


def _pool(*tickers: tuple[str, float | None]) -> dict[str, list[dict[str, object]]]:
    """Build a longlist the way the selection writes it: percent, and no sector."""

    return {
        "longlist": [
            {
                "ticker": ticker,
                "name": ticker,
                "expected_return_pct": None if er is None else round(er * 100, 2),
            }
            for ticker, er in tickers
        ]
    }


def _recommendation_pool(*tickers: tuple[str, float | None]) -> dict[str, list[dict[str, object]]]:
    """Build a recommendations pool, which states the same estimate as a ratio."""

    return {
        "recommendations": [
            {"ticker": ticker, "name": ticker, "sector_33": "情報・通信業", "er_annual": er}
            for ticker, er in tickers
        ]
    }


def _reading(*series: dict[str, object]) -> dict[str, object]:
    return {"asof": "2026-07-29", "rules_revision": "r", "series": list(series)}


def _delta_pair(
    latest_pool: dict[str, list[dict[str, object]]],
    previous_pool: dict[str, list[dict[str, object]]],
    *,
    latest_rules: str | None = "rules-a",
    previous_rules: str | None = "rules-a",
) -> StubDeltaCandidates:
    latest = _delta_run(asof=date(2026, 7, 29), revision="run-b", rules_ref=latest_rules)
    previous = _delta_run(asof=date(2026, 7, 28), revision="run-a", rules_ref=previous_rules)
    return StubDeltaCandidates(latest, previous, {"run-b": latest_pool, "run-a": previous_pool})


def test_daily_delta_compares_the_machine_pool_not_the_evaluated_universe() -> None:
    # The run's candidate array is the whole universe, so comparing it would report
    # listings and delistings. The pool a human reviews is what the delta must use.
    candidates = _delta_pair(
        _pool(("1111", 0.08), ("2222", 0.05)), _pool(("2222", 0.05), ("3333", 0.02))
    )
    market = StubDeltaMarket(disclosures={"1111": date(2026, 7, 29)})

    view = build_daily_delta(
        candidates, StubLedger(None), StubResearch([]), market, StubDeltaMacro({})
    )

    assert view.pool == "longlist"
    assert [item.ticker for item in view.entered] == ["1111"]
    assert view.entered[0].disclosed_since_previous is True
    assert view.entered[0].er_annual_pct == 8.0
    assert [item.ticker for item in view.exited] == ["3333"]
    # The disclosure window starts at the earlier run's as-of, not today.
    assert market.disclosure_afters == [date(2026, 7, 28)]


def test_daily_delta_reads_the_two_pools_estimate_units_as_the_same_quantity() -> None:
    # The longlist states percent and a recommendation states the ratio. Reading one
    # for the other would report an 8% estimate as 800%, or as 0.08%.
    longlist = _delta_pair(_pool(("1111", 0.08)), _pool(("3333", 0.02)))
    recommendations = _delta_pair(
        _recommendation_pool(("1111", 0.08)), _recommendation_pool(("3333", 0.02))
    )

    from_longlist = build_daily_delta(
        longlist, StubLedger(None), StubResearch([]), StubDeltaMarket(), StubDeltaMacro({})
    )
    from_recommendations = build_daily_delta(
        recommendations, StubLedger(None), StubResearch([]), StubDeltaMarket(), StubDeltaMacro({})
    )

    assert from_longlist.pool == "longlist"
    assert from_recommendations.pool == "recommendations"
    assert from_longlist.entered[0].er_annual_pct == 8.0
    assert from_recommendations.entered[0].er_annual_pct == 8.0


def test_daily_delta_names_the_estimate_gap_when_the_pool_carries_none() -> None:
    # A pool this reader cannot take an estimate from produces no mover, and an empty
    # mover list is indistinguishable from a quiet day unless the gap is named.
    candidates = _delta_pair(
        {"longlist": [{"ticker": "1111", "name": "1111"}, {"ticker": "2222", "name": "2222"}]},
        {"longlist": [{"ticker": "2222", "name": "2222"}, {"ticker": "3333", "name": "3333"}]},
    )

    view = build_daily_delta(
        candidates, StubLedger(None), StubResearch([]), StubDeltaMarket(), StubDeltaMacro({})
    )

    assert "candidates_estimate" in view.unavailable
    # The pool comparison itself still works; only the estimate is missing.
    assert [item.ticker for item in view.entered] == ["1111"]
    assert view.er_moves == []
    assert view.er_moves_total == 0


def test_daily_delta_reports_no_pool_when_neither_run_published_one() -> None:
    latest = _delta_run(asof=date(2026, 7, 29), revision="run-b")
    previous = _delta_run(asof=date(2026, 7, 28), revision="run-a")

    view = build_daily_delta(
        StubDeltaCandidates(latest, previous),
        StubLedger(None),
        StubResearch([]),
        StubDeltaMarket(),
        StubDeltaMacro({}),
    )

    assert view.pool is None
    assert "candidates_pool" in view.unavailable


def test_daily_delta_withholds_pool_rows_when_the_two_runs_used_different_rules() -> None:
    # A rules revision replaces the pool wholesale, so the difference is a method
    # change and must not read as a market change.
    candidates = _delta_pair(_pool(("1111", 0.08)), _pool(("3333", 0.02)), latest_rules="rules-b")

    view = build_daily_delta(
        candidates, StubLedger(None), StubResearch([]), StubDeltaMarket(), StubDeltaMacro({})
    )

    assert view.rules_changed is True
    assert view.entered == []
    assert view.exited == []


def test_daily_delta_reports_only_er_moves_past_the_threshold() -> None:
    candidates = _delta_pair(
        _pool(("1111", 0.08), ("2222", 0.06)), _pool(("1111", 0.02), ("2222", 0.05))
    )

    view = build_daily_delta(
        candidates, StubLedger(None), StubResearch([]), StubDeltaMarket(), StubDeltaMacro({})
    )

    assert [(item.ticker, item.change_pp) for item in view.er_moves] == [("1111", 6.0)]
    assert view.er_moves_total == 1


def test_daily_delta_names_the_market_store_and_leaves_disclosure_unknown() -> None:
    # Without the market store the disclosure fact is unknown, which must not read as
    # "no disclosure", and the holdings comparison is not attempted at all.
    candidates = _delta_pair(_pool(("1111", 0.08)), _pool(("3333", 0.02)))

    view = build_daily_delta(
        candidates,
        StubLedger(_snapshot()),
        StubResearch([_revision()]),
        StubDeltaMarket(exists=False),
        StubDeltaMacro({}),
    )

    assert "market" in view.unavailable
    assert view.entered[0].disclosed_since_previous is None
    assert view.holdings == []


def test_daily_delta_marks_a_holding_at_or_above_its_recorded_fair_value() -> None:
    candidates = _delta_pair(_pool(), _pool())
    market = StubDeltaMarket(
        closes={"4432": (1400.0, date(2026, 7, 29))},
        changes={"4432": 0.7},
    )

    view = build_daily_delta(
        candidates, StubLedger(_snapshot()), StubResearch([_revision()]), market, StubDeltaMacro({})
    )

    assert [item.ticker for item in view.holdings] == ["4432"]
    assert view.holdings[0].at_or_above_fair_value is True
    assert view.holdings_without_fair_value == 0
    assert view.holdings_without_price == 0
    # The earlier side is pinned to the previous run's as-of, not to "latest".
    assert market.change_sinces == [date(2026, 7, 28)]


def test_daily_delta_counts_holdings_that_cannot_be_compared() -> None:
    candidates = _delta_pair(_pool(), _pool())

    without_fv = build_daily_delta(
        candidates,
        StubLedger(_snapshot()),
        StubResearch([]),
        StubDeltaMarket(closes={"4432": (1200.0, date(2026, 7, 29))}),
        StubDeltaMacro({}),
    )
    without_price = build_daily_delta(
        candidates,
        StubLedger(_snapshot()),
        StubResearch([_revision()]),
        StubDeltaMarket(),
        StubDeltaMacro({}),
    )

    assert without_fv.holdings == []
    assert without_fv.holdings_without_fair_value == 1
    assert without_price.holdings == []
    assert without_price.holdings_without_price == 1


def test_daily_delta_reports_a_flag_that_appeared_and_one_that_cleared() -> None:
    candidates = _delta_pair(_pool(), _pool())
    readings = {
        date(2026, 7, 29): _reading({"series_id": "vix", "flags": ["vix>=30"], "z_score": 1.0}),
        date(2026, 7, 28): _reading(
            {"series_id": "vix", "flags": ["curve_inverted"], "z_score": 1.0}
        ),
    }

    view = build_daily_delta(
        candidates,
        StubLedger(None),
        StubResearch([]),
        StubDeltaMarket(),
        StubDeltaMacro(readings),
    )

    assert [(item.flag, item.state) for item in view.macro_flags] == [
        ("vix>=30", "raised"),
        ("curve_inverted", "cleared"),
    ]


def test_daily_delta_reports_only_a_new_distribution_edge() -> None:
    candidates = _delta_pair(_pool(), _pool())
    readings = {
        date(2026, 7, 29): _reading(
            {"series_id": "new", "flags": [], "z_score": 3.4},
            {"series_id": "settled", "flags": [], "z_score": 3.9},
            # Oscillating across the line is not arriving at the edge.
            {"series_id": "flutter", "flags": [], "z_score": 3.02},
        ),
        date(2026, 7, 28): _reading(
            {"series_id": "new", "flags": [], "z_score": 2.1},
            {"series_id": "settled", "flags": [], "z_score": 3.5},
            {"series_id": "flutter", "flags": [], "z_score": 2.97},
        ),
    }

    view = build_daily_delta(
        candidates,
        StubLedger(None),
        StubResearch([]),
        StubDeltaMarket(),
        StubDeltaMacro(readings),
    )

    assert [item.series_id for item in view.macro_extremes] == ["new"]
    assert view.macro_extremes[0].previous_z_score == 2.1


def test_daily_delta_macro_survives_a_missing_previous_run() -> None:
    # The macro reading is a pure function of its own store, so a screening outage
    # must not hide the fastest-moving signal.
    latest = _delta_run(asof=date(2026, 7, 29), revision="run-b")
    readings = {
        date(2026, 7, 29): _reading({"series_id": "vix", "flags": ["vix>=30"], "z_score": 1.0}),
        date(2026, 7, 28): _reading({"series_id": "vix", "flags": [], "z_score": 1.0}),
    }

    view = build_daily_delta(
        StubDeltaCandidates(latest, None),
        StubLedger(None),
        StubResearch([]),
        StubDeltaMarket(),
        StubDeltaMacro(readings),
    )

    assert "candidates_previous_run" in view.unavailable
    assert "macro" not in view.unavailable
    assert [item.flag for item in view.macro_flags] == ["vix>=30"]


def test_daily_delta_names_the_sections_no_store_could_answer() -> None:
    # An empty section and an unmeasured one must not read the same.
    view = build_daily_delta(
        StubDeltaCandidates(None, None),
        StubLedger(None),
        StubResearch([]),
        StubDeltaMarket(previous_business=None),
        StubDeltaMacro({}),
    )

    assert view.unavailable == ["candidates", "holdings", "macro"]
    assert view.previous_asof is None
