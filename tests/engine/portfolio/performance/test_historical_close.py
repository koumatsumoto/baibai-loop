"""過去の実績も、その営業日の引けまでに生じた時刻付き事実だけを含める。"""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
import yaml
from tests.engine.portfolio.performance.test_calculate import (
    _benchmark,
    _ledger,
    _prepare_outcome_cli,
)
from tests.helpers.db_seed import seed_ledger
from tests.helpers.screening_sqlite import add_source_coverage, seed_daily_bars

from baibai_engine.cli import portfolio as portfolio_cli
from baibai_engine.machine_analysis.observations.prices.benchmark_observation import (
    BenchmarkObservation,
)
from baibai_engine.machine_analysis.observations.prices.price_models import JQuantsDailyBar
from baibai_engine.portfolio.ledger.drafts import (
    apply_draft,
    build_broker_fact_draft,
    build_event_draft,
    build_sell_execution_draft,
)
from baibai_engine.portfolio.ledger.events import (
    CostEvent,
    PortfolioLedgerDocument,
    replay_events_through,
)
from baibai_engine.portfolio.ledger.store import LedgerStoreService
from baibai_engine.portfolio.performance import calculate
from baibai_engine.portfolio.performance.calculate import (
    OutcomePeriod,
    compute_portfolio_outcome,
    outcome_to_payload,
)
from baibai_engine.portfolio.performance.store import (
    PortfolioOutcomeConflictError,
    PortfolioOutcomePublication,
    PortfolioOutcomeStore,
)


def benchmark(end="2024-11-01"):
    return BenchmarkObservation.model_validate(
        {
            "schema_version": 1,
            "kind": "benchmark_observation",
            "benchmark_id": "jpx-topix-gross-total-return",
            "index_name": "TOPIX",
            "dividend_treatment": "gross_total_return",
            "horizon": "1y",
            "period_start_date": "2023-11-02",
            "period_end_date": end,
            "period_basis": "official_explicit",
            "period_rule_source_url": None,
            "cumulative_return_pct": 0.0,
            "annualized_return_pct": None,
            "display_precision_bps": 0,
            "source_url": "https://www.jpx.co.jp/synthetic-observation",
            "source_as_of": end,
            "published_at": "2024-11-06",
            "retrieved_at": "2024-11-07T00:00:00+09:00",
        }
    )


def outcome(*, end="2024-11-01", clock="15:10:00", opening_clock="09:00:00", reported=False):
    opening = {
        "event_id": "opening",
        "type": "opening_balance",
        "occurred_at": f"2023-11-02T{opening_clock}+09:00",
        "amount_yen": 1000,
    }
    cost = {
        "event_id": "cost",
        "type": "cost",
        "occurred_at": f"{end}T{clock}+09:00",
        "cost_kind": "commission",
        "amount_yen": 10,
    }
    if reported:
        cost["occurred_on"] = end
    ledger = PortfolioLedgerDocument.model_validate(
        {
            "schema_version": 3,
            "portfolio_scope": "repository_only",
            "as_of": f"{end}T18:00:00+09:00",
            "events": [opening, cost],
        }
    )
    return compute_portfolio_outcome(
        ledger,
        benchmark(end),
        business_days=(date(2023, 11, 2), date.fromisoformat(end)),
        bars=(),
    )


@pytest.mark.parametrize(
    ("end", "clock", "expected_cost"),
    [
        ("2024-11-01", "14:59:59", 10),
        ("2024-11-01", "15:00:00", 10),
        ("2024-11-01", "15:00:01", 0),
        ("2024-11-01", "15:10:00", 0),
        ("2024-11-05", "15:10:00", 10),
        ("2024-11-05", "15:30:00", 10),
        ("2024-11-05", "15:30:01", 0),
    ],
)
def test_confirmed_cost_uses_its_session_close(end, clock, expected_cost):
    result = outcome(end=end, clock=clock)
    assert result.status == "resolved"
    assert result.confirmed_cost_yen == expected_cost
    assert result.ending_cash_yen == 1000 - expected_cost
    assert result.portfolio_twr_pct == pytest.approx(-expected_cost / 10)


def test_reported_day_keeps_its_date_only_meaning_after_the_clock_rule_changes():
    result = outcome(reported=True)
    assert result.confirmed_cost_yen == 10
    assert result.ending_cash_yen == 990


@pytest.mark.parametrize(
    ("clock", "status"), [("15:00:00", "resolved"), ("15:00:01", "unresolved")]
)
def test_opening_balance_must_also_exist_by_the_historical_opening_close(clock, status):
    assert outcome(opening_clock=clock).status == status


@pytest.mark.parametrize(
    ("order_clock", "date_only", "included", "status"),
    [
        ("14:00:00", True, True, "filled"),
        ("15:30:00", True, True, "filled"),
        ("17:00:00", True, False, "filled"),
        ("17:00:00", False, False, "filled"),
        ("17:00:00", True, False, "cancelled"),
    ],
)
def test_reserved_buy_uses_only_known_clock_as_close_bound(
    tmp_path, order_clock, date_only, included, status
):
    benchmark = _benchmark()
    start, end = benchmark.period_start_date, benchmark.period_end_date
    path = tmp_path / "app.sqlite"
    source = _ledger()
    seed_ledger(path, source.model_copy(update={"events": source.events[:1]}))
    service = LedgerStoreService(path)
    order_at = datetime.fromisoformat(f"{end}T{order_clock}+09:00")
    later_fact_at = datetime.fromisoformat(f"{end}T16:00:00+09:00")
    terms = dict(
        direct_fact=True,
        decision_reference=None,
        ticker="1234",
        quantity=100,
        order_id="synthetic-buy",
        now=order_at + timedelta(days=1),
    )
    order, _ = build_broker_fact_draft(
        service,
        lambda _: "1234",
        **terms,
        status="open",
        occurred_at=order_at,
        price_guard_yen=Decimal(5),
        sector="合成業種",
    )
    assert order is not None
    apply_draft(service, order, human_confirmed=True)
    # This unrelated exact entry pushes the synthetic execution ordering key past close,
    # even if the real order could already have filled by the close.
    fee = CostEvent(
        type="cost",
        event_id="unrelated",
        occurred_at=later_fact_at,
        cost_kind="commission",
        amount_yen=1,
    )
    apply_draft(service, build_event_draft(service, fee), human_confirmed=True)
    occurred = (
        {"occurred_on": end} if date_only else {"occurred_at": order_at + timedelta(minutes=1)}
    )
    terminal = (
        {"price_yen": Decimal(5), "execution_id": "synthetic-fill"} if status == "filled" else {}
    )
    fact, _ = build_broker_fact_draft(
        service,
        lambda _: "1234",
        **terms,
        **occurred,
        status=status,
        **terminal,
    )
    assert fact is not None
    apply_draft(service, fact, human_confirmed=True)
    saved = service.load()
    assert saved.events[-1].occurred_at > datetime.fromisoformat(f"{end}T15:30:00+09:00")
    assert saved.events[-1].occurred_on == (end if date_only else None)
    state = replay_events_through(saved.events, saved.as_of)
    assert (state.available_cash_yen, state.reserved_cash_yen) == (
        499 if status == "filled" else 999,
        0,
    )
    assert sum(lot.quantity for lot in state.lots["1234"]) == (100 if status == "filled" else 0)

    days = (start, end)
    period = OutcomePeriod(saved, days, start, end)
    scoped = period.ledger_at_close()
    assert tuple(event.type for event in scoped.events) == (
        ("opening_balance", "reservation", "execution") if included else ("opening_balance",)
    )
    assert period.market_tickers() == (("1234",) if included else ())
    result = period.compute(
        benchmark,
        bars=(JQuantsDailyBar("1234", end, 6.0, None, adjustment_factor=1.0),) if included else (),
    )
    assert result.status == "resolved"
    assert result.ending_cash_yen == (500 if included else 1000)
    assert result.ending_holdings_market_value_yen == (600 if included else 0)
    assert result.confirmed_cost_yen == 0
    assert result.portfolio_twr_pct == pytest.approx(10 if included else 0)
    assert service.load() == saved


@pytest.fixture
def date_only_sale(tmp_path):
    end = _benchmark().period_end_date
    report_day = end - timedelta(days=1)
    order_at = datetime.fromisoformat(f"{report_day}T17:00:00+09:00")
    path = tmp_path / "app.sqlite"
    source = _ledger()
    seed_ledger(path, source.model_copy(update={"events": source.events[:3]}))
    service = LedgerStoreService(path)
    order, _ = build_broker_fact_draft(
        service,
        lambda _: "1234",
        decision_reference="synthetic-sell",
        side="sell",
        status="open",
        occurred_at=order_at,
        now=order_at,
        ticker="1234",
        quantity=100,
        price_guard_yen=Decimal(6),
    )
    assert order is not None
    apply_draft(service, order, human_confirmed=True)
    reservation = order.replacement.events[-1]
    sell = build_sell_execution_draft(
        service,
        occurred_on=report_day,
        now=order_at,
        ticker="1234",
        quantity=100,
        price_yen=Decimal(6),
        reservation_id=reservation.reservation_id,
        execution_id="synthetic-reported-sale",
        fees_yen=10,
        tax_yen=20,
    )
    assert sell is not None
    apply_draft(service, sell, human_confirmed=True)
    independent = CostEvent(
        type="cost",
        event_id="independent-reported-fee",
        occurred_on=report_day,
        occurred_at=order_at,
        ticker="1234",
        cost_kind="commission",
        amount_yen=2,
    )
    apply_draft(service, build_event_draft(service, independent), human_confirmed=True)
    return service.load(), independent


def test_reserved_sell_and_its_charges_enter_together_after_the_known_order(date_only_sale):
    benchmark = _benchmark()
    start, end = benchmark.period_start_date, benchmark.period_end_date
    report_day = end - timedelta(days=1)
    saved, independent = date_only_sale
    days = (start, report_day, end)
    before_sale = OutcomePeriod(saved, days, start, report_day).ledger_at_close()
    assert before_sale.events == (*_ledger().events[:3], independent)
    period = OutcomePeriod(saved, days, start, end)
    assert period.ledger_at_close() == saved
    bars = (
        JQuantsDailyBar("1234", start, 5.0, None, adjustment_factor=1.0),
        JQuantsDailyBar("1234", report_day, 6.0, None, adjustment_factor=1.0),
    )
    result = period.compute(benchmark, bars=bars)
    assert result.status == "resolved"
    assert (result.ending_cash_yen, result.ending_holdings_market_value_yen) == (1068, 0)
    assert (result.confirmed_cost_yen, result.confirmed_tax_yen) == (12, 20)


def test_outcome_cli_reads_period_calendar_once_and_keeps_the_market_snapshot(
    tmp_path, date_only_sale, monkeypatch, capsys
):
    saved, _ = date_only_sale
    args, application = _prepare_outcome_cli(tmp_path, saved)
    market = tmp_path / "market.sqlite"
    benchmark = _benchmark()
    start, end = benchmark.period_start_date, benchmark.period_end_date
    report_day = end - timedelta(days=1)
    rows = [
        ("1234", day.isoformat(), 6.0 if day == report_day else 5.0, 1.0)
        for ordinal in range(start.toordinal(), end.toordinal() + 1)
        if (day := date.fromordinal(ordinal)).weekday() < 5
    ]
    seed_daily_bars(market, rows)
    with closing(sqlite3.connect(market)) as writer, writer:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        add_source_coverage(
            writer,
            source="jquants_daily_bars",
            coverage_key="date-only-sale-prices",
            min_date=start.isoformat(),
            max_date=end.isoformat(),
            record_count=len(rows),
        )

    trace = []
    connect = portfolio_cli.connect_read_only

    def traced_read(path):
        connection = connect(path)
        connection.set_trace_callback(trace.append)
        return connection

    assigned = []
    assign = calculate._effective_event_dates

    def traced_assignment(*args, **kwargs):
        assigned.append((kwargs["start"], kwargs["end"]))
        return assign(*args, **kwargs)

    read_calendar = portfolio_cli.read_market_calendar

    def read_calendar_then_correct(path, start, end, *, connection):
        calendar = read_calendar(path, start, end, connection=connection)
        # Commit after the period calendar, before the CLI reads closes and
        # holding basis. A fresh connection sees all three corrections.
        with closing(sqlite3.connect(market)) as writer, writer:
            writer.execute(
                "UPDATE jquants_market_calendar SET holiday_division = '0' WHERE day = ?",
                (report_day.isoformat(),),
            )
            writer.execute(
                "UPDATE jquants_daily_bars "
                "SET close = 999.0, adjustment_factor = 0.5, ex_right_type = '1' "
                "WHERE ticker = '1234' AND traded_at = ?",
                (report_day.isoformat(),),
            )
        return calendar

    monkeypatch.setattr(portfolio_cli, "connect_read_only", traced_read)
    monkeypatch.setattr(portfolio_cli, "read_market_calendar", read_calendar_then_correct)
    monkeypatch.setattr(calculate, "_effective_event_dates", traced_assignment)

    assert portfolio_cli.main(args) == 0
    result = yaml.safe_load(capsys.readouterr().out)
    assert result["status"] == "resolved"
    assert (result["ending_cash_yen"], result["ending_holdings_market_value_yen"]) == (1068, 0)
    assert (result["confirmed_cost_yen"], result["confirmed_tax_yen"]) == (12, 20)
    assert result["portfolio_twr_pct"] == pytest.approx(6.8)
    assert PortfolioOutcomeStore(application).list() == (result,)
    with closing(sqlite3.connect(market)) as connection:
        assert connection.execute(
            "SELECT holiday_division FROM jquants_market_calendar WHERE day = ?",
            (report_day.isoformat(),),
        ).fetchone() == ("0",)
        assert connection.execute(
            "SELECT close, adjustment_factor FROM jquants_daily_bars "
            "WHERE ticker = '1234' AND traded_at = ?",
            (report_day.isoformat(),),
        ).fetchone() == (999.0, 0.5)

    # The separate pre-sale price-basis range does not count as this period read.
    period_reads = sum(
        sql.startswith("SELECT day, holiday_division FROM jquants_market_calendar ")
        and f"BETWEEN '{start}' AND '{end}'" in sql
        for sql in trace
    )
    assert set(assigned) == {(start, end)}
    assert (period_reads, len(assigned)) == (1, 1)


@pytest.mark.parametrize(
    ("expiry_clock", "status", "released_by_close"),
    [
        ("17:00:00+09:00", "expired", False),
        ("08:00:00+00:00", "expired", False),
        ("15:30:00+09:00", "expired", True),
        ("14:30:00+09:00", "expired", True),
        ("17:00:00+09:00", "cancelled", True),
    ],
)
def test_known_expiry_bounds_date_only_expired_release_not_cancellation(
    tmp_path, expiry_clock, status, released_by_close
):
    benchmark = _benchmark()
    start, end = benchmark.period_start_date, benchmark.period_end_date
    path = tmp_path / "app.sqlite"
    source = _ledger()
    seed_ledger(path, source.model_copy(update={"events": source.events[:1]}))
    service = LedgerStoreService(path)
    open_at = datetime.fromisoformat(f"{end}T14:00:00+09:00")
    expiry_at = datetime.fromisoformat(f"{end}T{expiry_clock}")
    terms = dict(
        ticker="1234", decision_reference=None, direct_fact=True, order_id="confirmed-order"
    )
    order, _ = build_broker_fact_draft(
        service,
        lambda _: "1234",
        **terms,
        status="open",
        occurred_at=open_at,
        now=open_at,
        expires_at=expiry_at,
        price_guard_yen=Decimal(5),
        quantity=100,
        sector="合成業種",
    )
    assert order is not None
    apply_draft(service, order, human_confirmed=True)
    if status == "expired":
        with pytest.raises(ValueError, match="date-only expired report predates expires_at"):
            build_broker_fact_draft(
                service,
                lambda _: "1234",
                **terms,
                status=status,
                occurred_on=end,
                now=expiry_at - timedelta(microseconds=1),
            )
    draft, _ = build_broker_fact_draft(
        service,
        lambda _: "1234",
        **terms,
        status=status,
        occurred_on=end,
        now=datetime.fromisoformat(f"{end}T18:00:00+09:00"),
    )
    assert draft is not None
    apply_draft(service, draft, human_confirmed=True)
    saved = service.load()
    state = replay_events_through(saved.events, saved.as_of)
    assert (state.available_cash_yen, state.reserved_cash_yen) == (1000, 0)
    result = compute_portfolio_outcome(saved, benchmark, business_days=(start, end), bars=())
    assert result.status == "resolved"
    assert (result.ending_cash_yen, result.ending_reserved_cash_yen) == (
        (1000, 0) if released_by_close else (500, 500)
    )
    assert result.portfolio_twr_pct == 0
    following = end + timedelta(days=1)
    following_benchmark = benchmark.model_copy(
        update={"period_end_date": following, "source_as_of": following}
    )
    next_result = compute_portfolio_outcome(
        saved, following_benchmark, business_days=(start, end, following), bars=()
    )
    assert next_result.status == "resolved"
    assert (next_result.ending_cash_yen, next_result.ending_reserved_cash_yen) == (1000, 0)
    assert next_result.portfolio_twr_pct == 0


def test_recalculation_does_not_rewrite_an_outcome_saved_with_the_old_close_rule(tmp_path):
    current = outcome()
    historical = replace(
        current,
        confirmed_cost_yen=10,
        ending_cash_yen=990,
        portfolio_twr_pct=-1.0,
        excess_percentage_points=-1.0,
    )
    payload = outcome_to_payload(historical)
    payload.update(
        benchmark_id=benchmark().benchmark_id,
        benchmark_observation=benchmark().model_dump(mode="json"),
        market_data_ref="synthetic:historical-close",
        market_data_coverage_start_date=historical.period_start_date.isoformat(),
        market_data_coverage_end_date=historical.period_end_date.isoformat(),
    )
    saved = PortfolioOutcomePublication(
        outcome_id="old-close-result",
        horizon=historical.horizon,
        period_start_date=historical.period_start_date.isoformat(),
        period_end_date=historical.period_end_date.isoformat(),
        status=historical.status,
        payload=payload,
    )
    path = tmp_path / "application.sqlite"
    store = PortfolioOutcomeStore(path)
    assert store.publish(saved)
    original = store.list()[0]
    before = path.read_bytes()
    assert not store.publish(saved)
    corrected = replace(saved, payload={**payload, **outcome_to_payload(current)})
    with pytest.raises(PortfolioOutcomeConflictError):
        store.publish(corrected)
    assert path.read_bytes() == before
    assert store.list() == (original,)
    assert store.publish(replace(corrected, outcome_id="corrected-close-result"))
    rows = {row["outcome_id"]: row for row in store.list()}
    assert rows[saved.outcome_id] == original
    assert rows["corrected-close-result"]["ending_cash_yen"] == 1000
