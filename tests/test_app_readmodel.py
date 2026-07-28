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
    def __init__(self, run: CandidatesRun | None):
        self._run = run

    def latest_run(self):
        return self._run


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
    assert absent.next_event is not None
    assert absent.next_event.task_id == "task-29990102-event"
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


def test_bargain_score_is_none_when_both_er_components_missing() -> None:
    view = _candidate_row({"ticker": "0000", "metrics": {}})

    assert view.er_reversion_annual is None
    assert view.er_carry_annual is None
    assert view.bargain_score is None


def test_bargain_score_full_reversion_half_carry_less_flag_discount() -> None:
    with_flags = _candidate_row(
        {
            "ticker": "1234",
            "metrics": {"er_reversion_annual": 0.10, "er_carry_annual": 0.04},
            "split_adjustment_flag": True,
            "freshness_warnings": ["stale"],
        }
    )
    carry_only = _candidate_row({"ticker": "5678", "metrics": {"er_carry_annual": 0.06}})
    anomalous_carry = _candidate_row(
        {"ticker": "9012", "metrics": {"er_reversion_annual": -0.01, "er_carry_annual": 0.94}}
    )

    # 0.10 + 0.5*0.04 - 0.005*2 flags
    assert len(with_flags.data_quality_flags) == 2
    assert with_flags.bargain_score == 0.11
    # reversion missing counts as 0.0; carry enters at half weight, no flags
    assert carry_only.bargain_score == 0.03
    # a special-dividend / anomalous carry is clipped at 15% so it cannot dominate the order
    assert anomalous_carry.bargain_score == round(-0.01 + 0.5 * 0.15, 6)


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
