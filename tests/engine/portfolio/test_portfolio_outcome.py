from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from baibai_engine.cli.portfolio import main
from baibai_engine.machine_analysis.observations.prices.benchmark_observation import (
    BenchmarkObservation,
)
from baibai_engine.machine_analysis.observations.prices.price_models import JQuantsDailyBar
from baibai_engine.portfolio.holdings.market_observations import market_basis_is_confirmed
from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument, PortfolioLedgerError
from baibai_engine.portfolio.performance.calculate import (
    DailyNav,
    PortfolioOutcomeError,
    compute_portfolio_outcome,
    compute_time_weighted_return,
    outcome_to_payload,
)
from baibai_engine.portfolio.performance.store import (
    PortfolioOutcomePublication,
    PortfolioOutcomeStore,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def test_twr_neutralizes_beginning_of_day_contribution() -> None:
    result = compute_time_weighted_return(
        (
            DailyNav(date(2026, 1, 2), 1_000),
            DailyNav(date(2026, 1, 5), 1_210, external_flow_yen=100),
        ),
        horizon_years=1,
    )

    assert result.cumulative_return_pct == pytest.approx(10.0)
    assert result.annualized_return_pct == pytest.approx(10.0)


def test_twr_distinguishes_total_loss_from_an_unfunded_base() -> None:
    result = compute_time_weighted_return(
        (DailyNav(date(2026, 1, 2), 1_000), DailyNav(date(2026, 1, 5), 0)),
        horizon_years=1,
    )
    assert result.cumulative_return_pct == -100.0
    assert result.annualized_return_pct == -100.0


def test_twr_rejects_zero_nav_after_withdrawal() -> None:
    with pytest.raises(PortfolioOutcomeError, match="zero_or_negative_nav"):
        compute_time_weighted_return(
            (
                DailyNav(date(2026, 1, 2), 1_000),
                DailyNav(date(2026, 1, 5), 0, external_flow_yen=-1_000),
            )
        )


def test_twr_preserves_total_loss_through_zero_days_and_refunding() -> None:
    result = compute_time_weighted_return(
        (
            DailyNav(date(2026, 1, 2), 1_000),
            DailyNav(date(2026, 1, 5), 0),
            DailyNav(date(2026, 1, 6), 0),
            DailyNav(date(2026, 1, 7), 550, external_flow_yen=500),
            DailyNav(date(2026, 1, 8), 605),
        ),
        horizon_years=3,
    )
    assert result.cumulative_return_pct == -100.0
    assert result.annualized_return_pct == -100.0


@pytest.mark.parametrize(("nav_yen", "flow_yen"), [(1, 0), (-1, 0), (0, -1)])
def test_twr_rejects_invalid_changes_after_total_loss(nav_yen, flow_yen) -> None:
    with pytest.raises(PortfolioOutcomeError, match="zero_or_negative_nav"):
        compute_time_weighted_return(
            (
                DailyNav(date(2026, 1, 2), 1_000),
                DailyNav(date(2026, 1, 5), 0),
                DailyNav(date(2026, 1, 6), nav_yen, external_flow_yen=flow_yen),
            )
        )


def _benchmark() -> BenchmarkObservation:
    return BenchmarkObservation.model_validate(
        {
            "schema_version": 1,
            "kind": "benchmark_observation",
            "benchmark_id": "jpx-topix-gross-total-return",
            "index_name": "TOPIX",
            "dividend_treatment": "gross_total_return",
            "horizon": "1y",
            "period_start_date": "2025-01-06",
            "period_end_date": "2026-01-06",
            "period_basis": "official_explicit",
            "period_rule_source_url": None,
            "cumulative_return_pct": 8.0,
            "annualized_return_pct": None,
            "display_precision_bps": 10,
            "source_url": "https://www.jpx.co.jp/example",
            "source_as_of": "2026-01-06",
            "published_at": "2026-01-07",
            "retrieved_at": "2026-01-08T00:00:00+09:00",
        }
    )


def _ledger() -> PortfolioLedgerDocument:
    return PortfolioLedgerDocument.model_validate(
        {
            "schema_version": 3,
            "portfolio_scope": "repository_only",
            "as_of": "2026-01-06T16:00:00+09:00",
            "estimated_exit_tax_rate_bps": 2000,
            "estimated_exit_tax_basis": "ledger_fifo_gross_unrealized_gain",
            "events": [
                {
                    "event_id": "opening",
                    "type": "opening_balance",
                    "occurred_at": "2025-01-06T09:00:00+09:00",
                    "amount_yen": 1000,
                },
                {
                    "event_id": "reserve",
                    "type": "reservation",
                    "occurred_at": "2025-01-06T09:01:00+09:00",
                    "reservation_id": "r1",
                    "order_id": "o1",
                    "ticker": "1234",
                    "sector": "test",
                    "common_factors": [],
                    "quantity": 100,
                    "price_guard_yen": 5,
                    "expires_at": "2025-01-07T09:00:00+09:00",
                },
                {
                    "event_id": "buy",
                    "type": "execution",
                    "occurred_at": "2025-01-06T09:02:00+09:00",
                    "execution_id": "x1",
                    "reservation_id": "r1",
                    "ticker": "1234",
                    "side": "buy",
                    "quantity": 100,
                    "price_yen": 5,
                },
                {
                    "event_id": "contribution-weekend",
                    "type": "contribution",
                    "occurred_at": "2026-01-04T09:00:00+09:00",
                    "amount_yen": 100,
                },
                {
                    "event_id": "income",
                    "type": "income",
                    "occurred_at": "2026-01-06T09:00:00+09:00",
                    "ticker": "1234",
                    "income_kind": "dividend",
                    "amount_yen": 20,
                },
                {
                    "event_id": "fee",
                    "type": "cost",
                    "occurred_at": "2026-01-06T09:01:00+09:00",
                    "ticker": "1234",
                    "cost_kind": "commission",
                    "amount_yen": 10,
                },
                {
                    "event_id": "tax",
                    "type": "tax_confirmed",
                    "occurred_at": "2026-01-06T09:02:00+09:00",
                    "ticker": "1234",
                    "tax_kind": "dividend",
                    "amount_yen": 2,
                },
            ],
        }
    )


@pytest.mark.parametrize("loss_day", ["2026-01-05", "2026-01-06"])
def test_confirmed_cost_consuming_all_capital_is_resolved_not_missing(loss_day) -> None:
    raw = _ledger().model_dump(mode="json")
    raw["events"] = [
        raw["events"][0],
        {
            "event_id": "confirmed-cost",
            "type": "cost",
            "occurred_at": f"{loss_day}T09:00:00+09:00",
            "cost_kind": "commission",
            "amount_yen": 1_000,
        },
    ]
    result = compute_portfolio_outcome(
        PortfolioLedgerDocument.model_validate(raw),
        _benchmark(),
        business_days=(date(2025, 1, 6), date(2026, 1, 5), date(2026, 1, 6)),
        bars=(),
    )
    assert result.status == "resolved"
    assert result.reason is None
    assert result.portfolio_twr_pct == -100.0
    assert result.ending_cash_yen == 0
    assert result.confirmed_cost_yen == 1_000


def test_portfolio_outcome_includes_idle_cash_and_confirmed_cashflows() -> None:
    from baibai_engine.portfolio.performance.calculate import compute_portfolio_outcome

    result = compute_portfolio_outcome(
        _ledger(),
        _benchmark(),
        business_days=(date(2025, 1, 6), date(2026, 1, 6)),
        bars=(
            JQuantsDailyBar("1234", date(2025, 1, 6), 5.0, None, adjustment_factor=1.0),
            JQuantsDailyBar("1234", date(2026, 1, 6), 6.0, None, adjustment_factor=1.0),
        ),
    )

    assert result.status == "resolved"
    assert result.portfolio_twr_pct == pytest.approx((1208 / 1100 - 1) * 100)
    assert result.excess_percentage_points == pytest.approx((1208 / 1100 - 1) * 100 - 8)
    assert result.confirmed_income_yen == 20
    assert result.confirmed_cost_yen == 10
    assert result.confirmed_tax_yen == 2
    # Estimated future exit tax is reported but must not reduce the realized NAV.
    assert result.estimated_exit_tax_yen == 20


@pytest.mark.parametrize("factor", [None, 0.5, 2.0])
def test_portfolio_outcome_rejects_unresolved_corporate_action(factor) -> None:
    from baibai_engine.portfolio.performance.calculate import compute_portfolio_outcome

    result = compute_portfolio_outcome(
        _ledger(),
        _benchmark(),
        business_days=(date(2025, 1, 6), date(2026, 1, 6)),
        bars=(
            JQuantsDailyBar("1234", date(2025, 1, 6), 5.0, None, adjustment_factor=1.0),
            JQuantsDailyBar("1234", date(2026, 1, 6), 6.0, None, adjustment_factor=factor),
        ),
    )

    assert result.status == "unresolved"
    assert result.reason == "corporate_action_unresolved"


def test_portfolio_outcome_does_not_assume_zero_factor_is_neutral() -> None:
    result = compute_portfolio_outcome(
        _ledger(),
        _benchmark(),
        business_days=(date(2025, 1, 6), date(2026, 1, 6)),
        bars=(
            JQuantsDailyBar("1234", date(2025, 1, 6), 5.0, None, adjustment_factor=1.0),
            JQuantsDailyBar("1234", date(2026, 1, 6), 6.0, None, adjustment_factor=0.0),
        ),
    )
    assert result.status == "unresolved"
    assert result.reason == "corporate_action_unresolved"


@pytest.mark.parametrize("tax_rate", [None, 2000])
def test_calculated_resolved_outcome_publishes_and_reads_without_inventing_exit_tax(
    tmp_path: Path, tax_rate
) -> None:
    ledger = _ledger().model_copy(
        update={
            "estimated_exit_tax_rate_bps": tax_rate,
            "estimated_exit_tax_basis": None
            if tax_rate is None
            else "ledger_fifo_gross_unrealized_gain",
        }
    )
    benchmark = _benchmark()
    result = compute_portfolio_outcome(
        ledger,
        benchmark,
        business_days=(date(2025, 1, 6), date(2026, 1, 6)),
        bars=(
            JQuantsDailyBar("1234", date(2025, 1, 6), 5.0, None, adjustment_factor=1.0),
            JQuantsDailyBar("1234", date(2026, 1, 6), 6.0, None, adjustment_factor=1.0),
        ),
    )
    assert result.status == "resolved"
    payload = outcome_to_payload(result)
    payload.update(
        benchmark_id=benchmark.benchmark_id,
        benchmark_observation=benchmark.model_dump(mode="json"),
        market_data_ref="synthetic:two-business-day-bars",
        market_data_coverage_start_date=result.period_start_date.isoformat(),
        market_data_coverage_end_date=result.period_end_date.isoformat(),
    )
    publication = PortfolioOutcomePublication(
        outcome_id="outcome-1y-2026-01-06",
        horizon=result.horizon,
        period_start_date=result.period_start_date.isoformat(),
        period_end_date=result.period_end_date.isoformat(),
        status=result.status,
        payload=payload,
    )
    database = tmp_path / "app.sqlite"
    assert PortfolioOutcomeStore(database).publish(publication)
    before = database.read_bytes()

    saved = PortfolioOutcomeStore(database).list()[0]

    assert saved["status"] == "resolved"
    assert saved["portfolio_twr_pct"] == result.portfolio_twr_pct
    assert saved["estimated_exit_tax_status"] == ("unknown" if tax_rate is None else "estimated")
    assert saved["estimated_exit_tax_yen"] == (None if tax_rate is None else 20)
    assert database.read_bytes() == before


def _lapsed_reservation_ledger() -> PortfolioLedgerDocument:
    """A reservation that lapses during the session and is reported that evening."""

    return PortfolioLedgerDocument.model_validate(
        {
            "schema_version": 3,
            "portfolio_scope": "repository_only",
            "as_of": "2026-01-06T16:00:00+09:00",
            "events": [
                {
                    "event_id": "opening",
                    "type": "opening_balance",
                    "occurred_at": "2025-01-06T09:00:00+09:00",
                    "amount_yen": 1_000_000,
                },
                {
                    "event_id": "reserve",
                    "type": "reservation",
                    "occurred_at": "2026-01-05T09:00:00+09:00",
                    "reservation_id": "r1",
                    "order_id": "o1",
                    "ticker": "1234",
                    "sector": "test",
                    "common_factors": [],
                    "quantity": 100,
                    "price_guard_yen": 500,
                    "expires_at": "2026-01-06T15:00:00+09:00",
                },
                {
                    "event_id": "release",
                    "type": "release",
                    "occurred_at": "2026-01-06T16:00:00+09:00",
                    "reservation_id": "r1",
                    "reason": "expired",
                },
            ],
        }
    )


def test_outcome_values_a_lapsed_reservation_reported_after_close() -> None:
    from baibai_engine.portfolio.performance.calculate import compute_portfolio_outcome

    result = compute_portfolio_outcome(
        _lapsed_reservation_ledger(),
        _benchmark(),
        business_days=(date(2025, 1, 6), date(2026, 1, 5), date(2026, 1, 6)),
        bars=(),
    )

    assert result.status == "resolved"
    # A release only moves yen between reserved and available, so NAV never moves.
    assert result.portfolio_twr_pct == pytest.approx(0.0)
    assert result.ending_cash_yen == 950_000
    assert result.ending_reserved_cash_yen == 50_000


def _unreleased_ledger() -> PortfolioLedgerDocument:
    raw = _lapsed_reservation_ledger().model_dump(mode="json")
    raw["events"] = [event for event in raw["events"] if event["event_id"] != "release"]
    return PortfolioLedgerDocument.model_validate(raw)


def test_an_outcome_is_not_published_from_a_ledger_awaiting_a_human_report() -> None:
    """The tolerance that lets a prefix replay through a lapse must not reach publish:
    an outcome row cannot be corrected once written."""

    from baibai_engine.portfolio.ledger.events import (
        replay_events_through,
        require_resolved_expiries,
    )

    ledger = _unreleased_ledger()
    state = replay_events_through(ledger.events, ledger.as_of)

    with pytest.raises(PortfolioLedgerError, match="expired reservations require"):
        require_resolved_expiries(state)


def test_after_close_internal_cashflow_rolls_past_that_close() -> None:
    from baibai_engine.portfolio.performance.calculate import compute_portfolio_outcome

    raw = _ledger().model_dump(mode="json")
    # Move the confirmed tax after close; it must not affect this period's NAV.
    raw["events"][6]["occurred_at"] = "2026-01-06T16:00:00+09:00"
    ledger = PortfolioLedgerDocument.model_validate(raw)
    result = compute_portfolio_outcome(
        ledger,
        _benchmark(),
        business_days=(date(2025, 1, 6), date(2026, 1, 6)),
        bars=(
            JQuantsDailyBar("1234", date(2025, 1, 6), 5.0, None, adjustment_factor=1.0),
            JQuantsDailyBar("1234", date(2026, 1, 6), 6.0, None, adjustment_factor=1.0),
        ),
    )

    assert result.status == "resolved"
    assert result.confirmed_tax_yen == 0


def test_outcome_cli_requires_an_imported_application_db_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    observation = tmp_path / "topix.yaml"
    observation.write_text(
        (
            Path(__file__).parents[2] / "fixtures" / "benchmark-observation" / "topix-1y.yaml"
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    assert main(["outcome", "--root", str(tmp_path), "--benchmark-observation", "topix.yaml"]) == 2
    assert "ledger has not been imported" in capsys.readouterr().err


@pytest.mark.parametrize("replay_time", ["00:00:00", "16:00:00", "23:59:59"])
def test_date_only_outcome_is_independent_of_internal_replay_time(replay_time: str) -> None:
    raw = _ledger().model_dump(mode="json")
    for event in raw["events"]:
        day = event["occurred_at"][:10]
        event["occurred_on"] = day
        event["occurred_at"] = f"{day}T{replay_time}+09:00"
    raw["as_of"] = "2026-01-06T23:59:59+09:00"
    ledger = PortfolioLedgerDocument.model_validate(raw)
    days = (date(2025, 1, 6), date(2026, 1, 6))
    bars = (
        JQuantsDailyBar("1234", days[0], 5.0, None, adjustment_factor=1.0),
        JQuantsDailyBar("1234", days[1], 6.0, None, adjustment_factor=1.0),
    )
    expected = compute_portfolio_outcome(_ledger(), _benchmark(), business_days=days, bars=bars)
    actual = compute_portfolio_outcome(ledger, _benchmark(), business_days=days, bars=bars)
    assert actual == expected
    assert actual.status == "resolved"
    assert actual.confirmed_income_yen == 20
    assert actual.confirmed_cost_yen == 10
    assert actual.confirmed_tax_yen == 2


def test_date_only_release_uses_reported_day_even_with_evening_replay_key() -> None:
    raw = _lapsed_reservation_ledger().model_dump(mode="json")
    raw["events"][-1]["occurred_on"] = "2026-01-06"
    result = compute_portfolio_outcome(
        PortfolioLedgerDocument.model_validate(raw),
        _benchmark(),
        business_days=(date(2025, 1, 6), date(2026, 1, 5), date(2026, 1, 6)),
        bars=(),
    )
    assert result.status == "resolved"
    assert result.portfolio_twr_pct == pytest.approx(0)
    assert result.ending_cash_yen == 1_000_000
    assert result.ending_reserved_cash_yen == 0


def test_period_cashflows_exclude_events_already_in_the_opening_close() -> None:
    raw = _ledger().model_dump(mode="json")
    opening = raw["events"][0]
    raw["events"] = [
        opening,
        {
            "event_id": "start-income",
            "type": "income",
            "occurred_on": "2025-01-06",
            "occurred_at": "2025-01-06T09:01:00+09:00",
            "income_kind": "dividend",
            "amount_yen": 10,
        },
        {
            "event_id": "start-cost",
            "type": "cost",
            "occurred_on": "2025-01-06",
            "occurred_at": "2025-01-06T09:02:00+09:00",
            "cost_kind": "commission",
            "amount_yen": 3,
        },
        {
            "event_id": "start-tax",
            "type": "tax_confirmed",
            "occurred_on": "2025-01-06",
            "occurred_at": "2025-01-06T09:03:00+09:00",
            "tax_kind": "dividend",
            "amount_yen": 2,
        },
        {
            "event_id": "end-cost",
            "type": "cost",
            "occurred_on": "2026-01-06",
            "occurred_at": "2026-01-06T09:00:00+09:00",
            "cost_kind": "commission",
            "amount_yen": 5,
        },
    ]
    result = compute_portfolio_outcome(
        PortfolioLedgerDocument.model_validate(raw),
        _benchmark(),
        business_days=(date(2025, 1, 6), date(2026, 1, 6)),
        bars=(),
    )
    assert result.status == "resolved"
    assert result.portfolio_twr_pct == pytest.approx((1_000 / 1_005 - 1) * 100)
    assert result.confirmed_income_yen == 0
    assert result.confirmed_cost_yen == 5
    assert result.confirmed_tax_yen == 0


def test_position_closed_before_opening_close_is_not_a_period_exit() -> None:
    raw = _ledger().model_dump(mode="json")
    raw["events"] = [
        *raw["events"][:3],
        {
            "event_id": "start-sell",
            "type": "execution",
            "execution_id": "start-sell",
            "occurred_at": "2025-01-06T09:03:00+09:00",
            "ticker": "1234",
            "side": "sell",
            "quantity": 100,
            "price_yen": 5,
        },
    ]
    result = compute_portfolio_outcome(
        PortfolioLedgerDocument.model_validate(raw),
        _benchmark(),
        business_days=(date(2025, 1, 6), date(2026, 1, 6)),
        bars=(),
    )
    assert result.status == "resolved"
    assert result.portfolio_twr_pct == 0.0
    assert result.closed_tickers == ()


def test_purchase_day_split_does_not_change_the_reported_fill_basis() -> None:
    result = compute_portfolio_outcome(
        _ledger(),
        _benchmark(),
        business_days=(date(2025, 1, 6), date(2026, 1, 6)),
        bars=(
            JQuantsDailyBar("1234", date(2025, 1, 6), 5.0, None, adjustment_factor=0.5),
            JQuantsDailyBar("1234", date(2026, 1, 6), 6.0, None, adjustment_factor=1.0),
        ),
    )
    assert result.status == "resolved"
    assert result.portfolio_twr_pct == pytest.approx((1208 / 1100 - 1) * 100)


def _preperiod_holding() -> PortfolioLedgerDocument:
    raw = _ledger().model_dump(mode="json")
    for event in raw["events"][:3]:
        event["occurred_at"] = event["occurred_at"].replace("2025-01-06", "2024-12-27")
    return PortfolioLedgerDocument.model_validate(raw)


def test_opening_holding_requires_evidence_of_its_preperiod_units() -> None:
    result = compute_portfolio_outcome(
        _preperiod_holding(),
        _benchmark(),
        business_days=(date(2025, 1, 6), date(2026, 1, 6)),
        bars=(
            JQuantsDailyBar("1234", date(2025, 1, 6), 5.0, None, adjustment_factor=1.0),
            JQuantsDailyBar("1234", date(2026, 1, 6), 6.0, None, adjustment_factor=1.0),
        ),
    )
    assert result.status == "unresolved"
    assert result.reason == "corporate_action_unresolved"


@pytest.mark.parametrize("preperiod_factor", [0.5, 1.0])
def test_opening_unit_check_uses_native_factors_not_historical_closes(tmp_path, preperiod_factor):
    database = tmp_path / "market.sqlite"
    with closing(open_connection(database)) as connection, connection:
        connection.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_factor) "
            "VALUES ('1234', ?, ?, ?)",
            [
                ("2024-12-30", None, preperiod_factor),
                ("2025-01-06", 5.0, 1.0),
                ("2026-01-06", 6.0, 1.0),
            ],
        )
        result = compute_portfolio_outcome(
            _preperiod_holding(),
            _benchmark(),
            business_days=(date(2025, 1, 6), date(2026, 1, 6)),
            bars=(
                JQuantsDailyBar("1234", date(2025, 1, 6), 5.0, None, adjustment_factor=1.0),
                JQuantsDailyBar("1234", date(2026, 1, 6), 6.0, None, adjustment_factor=1.0),
            ),
            confirm_price_basis=lambda ticker, first, last: market_basis_is_confirmed(
                basis="price",
                sqlite_path=database,
                ticker=ticker,
                from_date=first,
                through_date=last,
                connection=connection,
            ),
        )
    assert result.status == ("resolved" if preperiod_factor == 1.0 else "unresolved")
    assert result.reason == (None if preperiod_factor == 1.0 else "corporate_action_unresolved")


def test_full_sale_does_not_hide_a_corporate_action_on_its_execution_day() -> None:
    raw = _ledger().model_dump(mode="json")
    raw["events"] = [
        *raw["events"][:3],
        {
            "event_id": "sale",
            "type": "execution",
            "execution_id": "sale",
            "occurred_at": "2026-01-06T09:00:00+09:00",
            "ticker": "1234",
            "side": "sell",
            "quantity": 100,
            "price_yen": 6,
        },
    ]
    result = compute_portfolio_outcome(
        PortfolioLedgerDocument.model_validate(raw),
        _benchmark(),
        business_days=(date(2025, 1, 6), date(2026, 1, 6)),
        bars=(
            JQuantsDailyBar("1234", date(2025, 1, 6), 5.0, None, adjustment_factor=1.0),
            JQuantsDailyBar("1234", date(2026, 1, 6), 6.0, None, adjustment_factor=0.5),
        ),
    )
    assert result.status == "unresolved"
    assert result.reason == "corporate_action_unresolved"


def test_outcome_cli_publishes_cash_only_after_unfilled_order_without_price_coverage(
    tmp_path, monkeypatch, capsys
):
    import yaml
    from tests.helpers.screening_sqlite import add_source_coverage

    from baibai_engine.portfolio.ledger.store import LedgerStoreService

    # Exercise the CLI's ticker selection, actual calendar reader, calculation and
    # publication. The confirmed ledger contains a reservation but no acquisition.
    ledger = _lapsed_reservation_ledger()
    monkeypatch.setattr(LedgerStoreService, "load", lambda self: ledger)
    benchmark = _benchmark()
    observation = tmp_path / "benchmark.yaml"
    observation.write_text(yaml.safe_dump(benchmark.model_dump(mode="json")), encoding="utf-8")
    market = tmp_path / "market.sqlite"
    start, end = benchmark.period_start_date, benchmark.period_end_date
    days = [date.fromordinal(day) for day in range(start.toordinal(), end.toordinal() + 1)]
    with closing(open_connection(market)) as connection, connection:
        connection.executemany(
            "INSERT INTO jquants_market_calendar(day, holiday_division) VALUES (?, ?)",
            [(day.isoformat(), "1" if day.weekday() < 5 else "0") for day in days],
        )
        add_source_coverage(
            connection,
            source="jquants_market_calendar",
            coverage_key="fixture-calendar",
            min_date=start.isoformat(),
            max_date=end.isoformat(),
            record_count=len(days),
        )
    application = tmp_path / "application.sqlite"

    assert (
        main(
            [
                "outcome",
                "--db",
                str(application),
                "--sqlite",
                str(market),
                "--benchmark-observation",
                str(observation),
            ]
        )
        == 0
    )

    emitted = yaml.safe_load(capsys.readouterr().out)
    assert emitted["status"] == "resolved"
    assert emitted["portfolio_twr_pct"] == 0.0
    saved = PortfolioOutcomeStore(application).list()
    assert len(saved) == 1
    assert saved[0]["portfolio_twr_pct"] == 0.0
    assert saved[0]["ending_holdings_market_value_yen"] == 0
