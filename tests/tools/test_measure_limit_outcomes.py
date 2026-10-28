from __future__ import annotations

from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from tests.helpers.db_seed import seed_ledger

from baibai_engine.portfolio.ledger.events import PortfolioLedgerDocument
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from tools.experiments.measure_limit_outcomes import (
    POST_EXPIRY_SESSIONS,
    LimitOutcomeMeasurementError,
    build_limit_outcomes,
    main,
)

ASOF = date(2026, 7, 31)


def _ledger(path: Path, events: list[dict[str, object]]) -> None:
    document = PortfolioLedgerDocument.model_validate(
        {
            "schema_version": 3,
            "portfolio_scope": "repository_only",
            "as_of": "2026-07-31T15:30:00+09:00",
            "estimated_exit_tax_rate_bps": None,
            "estimated_exit_tax_basis": None,
            "events": [
                {
                    "event_id": "opening",
                    "type": "opening_balance",
                    "occurred_at": "2026-06-01T08:00:00+09:00",
                    "amount_yen": 5_000_000,
                },
                *events,
            ],
            "overrides": [],
        }
    )
    seed_ledger(path, document)


def _reservation(
    *,
    reservation_id: str,
    occurred_at: str,
    limit_yen: str,
    decision_reference: str | None,
) -> dict[str, object]:
    return {
        "event_id": f"event-{reservation_id}",
        "type": "reservation",
        "occurred_at": occurred_at,
        "reservation_id": reservation_id,
        "order_id": f"order-{reservation_id}",
        "ticker": "2331",
        "sector": "サービス業",
        "common_factors": [],
        "decision_reference": decision_reference,
        "quantity": 100,
        "price_guard_yen": limit_yen,
        "expires_at": "2026-07-31T15:30:00+09:00",
    }


def _market(path: Path, bars: list[tuple[str, float, float]]) -> None:
    connection = open_connection(path)
    try:
        connection.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, low, close, adjustment_factor) "
            "VALUES ('2331', ?, ?, ?, 1.0)",
            bars,
        )
        connection.commit()
    finally:
        connection.close()


def _post_expiry_sessions(
    *, high_close: float, sessions: int = POST_EXPIRY_SESSIONS
) -> list[tuple[str, float, float]]:
    """Bars after the 2026-07-10 expiry, the last one carrying the highest close."""

    days = [date(2026, 7, 13) + timedelta(days=offset) for offset in range(sessions)]
    return [
        (day.isoformat(), 1200.0, high_close if index == sessions - 1 else 1210.0)
        for index, day in enumerate(days)
    ]


def test_an_expired_order_reports_how_far_the_low_stayed_above_the_limit(tmp_path: Path) -> None:
    app_db = tmp_path / "app.sqlite"
    market = tmp_path / "market.sqlite"
    _ledger(
        app_db,
        [
            _reservation(
                reservation_id="res-1",
                occurred_at="2026-07-01T09:00:00+09:00",
                limit_yen="1000",
                decision_reference="issue-1",
            ),
            {
                "event_id": "release-1",
                "type": "release",
                "occurred_at": "2026-07-10T15:30:00+09:00",
                "reservation_id": "res-1",
                "reason": "expired",
            },
        ],
    )
    _market(
        market,
        [
            ("2026-07-02", 1100.0, 1150.0),
            ("2026-07-09", 1050.0, 1080.0),
            *_post_expiry_sessions(high_close=1250.0),
        ],
    )

    before = build_limit_outcomes(app_db=app_db, market_db=market, asof=ASOF)
    assert before["orders"][0]["post_expiry_sessions_observed"] == 19
    assert before["orders"][0]["forgone_pct"] is None
    payload = build_limit_outcomes(app_db=app_db, market_db=market, asof=date(2026, 8, 1))

    order = payload["orders"][0]
    assert order["outcome"] == "expired"
    assert order["window_low_yen"] == pytest.approx(1050.0)
    # 窓内の最安値 1,050 は指値 1,000 を 5% 上回った。
    assert order["distance_to_limit_pct"] == pytest.approx(5.0)
    assert order["post_expiry_sessions_observed"] == POST_EXPIRY_SESSIONS
    # 失効後の最高終値 1,250 は指値の +25%。
    assert order["forgone_pct"] == pytest.approx(25.0)


def test_a_forgone_move_is_absent_while_the_post_expiry_window_is_incomplete(
    tmp_path: Path,
) -> None:
    """4 日ぶんの上昇を 20 日窓の「逃した幅」として出さない。

    出すと、まだ測っていないものが測り終えた値の顔で中央値へ入り、直近の失効ほど
    「逃した幅が小さい」側へ寄る。
    """

    app_db = tmp_path / "app.sqlite"
    market = tmp_path / "market.sqlite"
    _ledger(
        app_db,
        [
            _reservation(
                reservation_id="res-1",
                occurred_at="2026-07-01T09:00:00+09:00",
                limit_yen="1000",
                decision_reference="issue-1",
            ),
            {
                "event_id": "release-1",
                "type": "release",
                "occurred_at": "2026-07-10T15:30:00+09:00",
                "reservation_id": "res-1",
                "reason": "expired",
            },
        ],
    )
    _market(
        market,
        [
            ("2026-07-09", 1050.0, 1080.0),
            *_post_expiry_sessions(high_close=1250.0, sessions=4),
        ],
    )

    payload = build_limit_outcomes(app_db=app_db, market_db=market, asof=ASOF)

    order = payload["orders"][0]
    assert order["post_expiry_sessions_observed"] == 4
    assert order["post_window_high_close_yen"] is None
    assert order["forgone_pct"] is None
    summary = payload["summary"]["all_ledger_orders"]
    assert summary["expired"] == 1
    assert summary["forgone_measured_orders"] == 0
    assert summary["forgone_pending_window_orders"] == 1
    assert summary["median_forgone_pct"] is None


def test_a_filled_order_is_not_charged_a_forgone_move(tmp_path: Path) -> None:
    app_db = tmp_path / "app.sqlite"
    market = tmp_path / "market.sqlite"
    _ledger(
        app_db,
        [
            _reservation(
                reservation_id="res-1",
                occurred_at="2026-07-01T09:00:00+09:00",
                limit_yen="1000",
                decision_reference="issue-1",
            ),
            {
                "event_id": "fill-1",
                "type": "execution",
                "occurred_at": "2026-07-02T09:00:00+09:00",
                "execution_id": "exec-1",
                "reservation_id": "res-1",
                "ticker": "2331",
                "side": "buy",
                "quantity": 100,
                "price_yen": "1000",
            },
        ],
    )
    _market(market, [("2026-07-02", 990.0, 1000.0), ("2026-07-15", 1200.0, 1250.0)])

    payload = build_limit_outcomes(app_db=app_db, market_db=market, asof=ASOF)

    order = payload["orders"][0]
    assert order["outcome"] == "filled"
    assert order["forgone_pct"] is None


def test_imported_orders_are_summarised_apart_from_decision_bound_orders(tmp_path: Path) -> None:
    """取り込み分の約定を規律の成績に数えると、fill 率が実態より良く見える。"""

    app_db = tmp_path / "app.sqlite"
    market = tmp_path / "market.sqlite"
    _ledger(
        app_db,
        [
            _reservation(
                reservation_id="migration-res",
                occurred_at="2026-07-01T09:00:00+09:00",
                limit_yen="1000",
                decision_reference=None,
            ),
            {
                "event_id": "fill-migration",
                "type": "execution",
                "occurred_at": "2026-07-02T09:00:00+09:00",
                "execution_id": "exec-migration",
                "reservation_id": "migration-res",
                "ticker": "2331",
                "side": "buy",
                "quantity": 100,
                "price_yen": "1000",
            },
            _reservation(
                reservation_id="res-decision",
                occurred_at="2026-07-03T09:00:00+09:00",
                limit_yen="900",
                decision_reference="issue-1",
            ),
            {
                "event_id": "release-decision",
                "type": "release",
                "occurred_at": "2026-07-10T15:30:00+09:00",
                "reservation_id": "res-decision",
                "reason": "expired",
            },
        ],
    )
    _market(market, [("2026-07-02", 990.0, 1000.0), ("2026-07-09", 1050.0, 1080.0)])

    payload = build_limit_outcomes(app_db=app_db, market_db=market, asof=ASOF)

    summary = payload["summary"]
    assert summary["all_ledger_orders"]["full_fill_rate_pct"] == pytest.approx(50.0)
    assert summary["decision_bound_orders"]["orders"] == 1
    assert summary["decision_bound_orders"]["full_fill_rate_pct"] == pytest.approx(0.0)
    assert "chase_policy_decision" not in payload


def test_a_missing_market_store_fails_instead_of_reporting_empty_windows(tmp_path: Path) -> None:
    app_db = tmp_path / "app.sqlite"
    _ledger(app_db, [])

    with pytest.raises(LimitOutcomeMeasurementError, match="market store"):
        build_limit_outcomes(app_db=app_db, market_db=tmp_path / "absent.sqlite", asof=ASOF)


@pytest.mark.parametrize("event_kind", ["reservation", "execution", "release"])
def test_events_after_cutoff_do_not_change_observed_order_state(
    tmp_path: Path, event_kind: str
) -> None:
    app_db, market = tmp_path / "app.sqlite", tmp_path / "market.sqlite"
    reservation = _reservation(
        reservation_id="res-cutoff",
        occurred_at="2026-07-31T09:00:00+09:00"
        if event_kind == "reservation"
        else "2026-07-01T09:00:00+09:00",
        limit_yen="1000",
        decision_reference="issue-cutoff",
    )
    events = [reservation]
    if event_kind == "execution":
        events.append(
            {
                "event_id": "fill-cutoff",
                "type": "execution",
                "occurred_at": "2026-07-31T09:00:00+09:00",
                "execution_id": "exec-cutoff",
                "reservation_id": "res-cutoff",
                "ticker": "2331",
                "side": "buy",
                "quantity": 100,
                "price_yen": "1000",
            }
        )
    elif event_kind == "release":
        events.append(
            {
                "event_id": "release-cutoff",
                "type": "release",
                "occurred_at": "2026-07-31T15:30:00+09:00",
                "reservation_id": "res-cutoff",
                "reason": "expired",
            }
        )
    _ledger(app_db, events)
    _market(market, [("2026-07-31", 990.0, 1000.0)])
    original = (app_db.read_bytes(), market.read_bytes())
    before = build_limit_outcomes(app_db=app_db, market_db=market, asof=date(2026, 7, 30))
    if event_kind == "reservation":
        assert before["orders"] == []
    else:
        assert before["orders"][0]["outcome"] == "open"
    after = build_limit_outcomes(app_db=app_db, market_db=market, asof=ASOF)
    assert (
        after["orders"][0]["outcome"]
        == {
            "reservation": "open",
            "execution": "filled",
            "release": "expired",
        }[event_kind]
    )
    assert (app_db.read_bytes(), market.read_bytes()) == original


def test_cli_writes_yaml_and_reports_an_unreadable_ledger(tmp_path: Path) -> None:
    app_db = tmp_path / "app.sqlite"
    market = tmp_path / "market.sqlite"
    _ledger(app_db, [])
    _market(market, [("2026-07-02", 990.0, 1000.0)])
    out = tmp_path / "limits.yaml"

    assert (
        main(
            [
                "--db",
                str(app_db),
                "--sqlite-path",
                str(market),
                "--asof",
                ASOF.isoformat(),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    payload = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert payload["kind"] == "limit-outcome-aggregate"

    assert (
        main(
            [
                "--db",
                str(tmp_path / "absent.sqlite"),
                "--sqlite-path",
                str(market),
                "--asof",
                ASOF.isoformat(),
            ]
        )
        == 1
    )


def test_broker_states_and_denominators_match_ledger_replay(tmp_path: Path) -> None:
    from datetime import datetime

    from baibai_engine.portfolio.ledger.events import replay_events_through
    from baibai_engine.portfolio.ledger.store import LedgerStoreService

    app_db, market = tmp_path / "app.sqlite", tmp_path / "market.sqlite"
    cases = [
        ("full", 200, None, "filled"),
        ("partial", 100, None, "partially_filled"),
        ("partial-cancel", 100, "cancelled", "cancelled"),
        ("partial-expire", 100, "expired", "expired"),
        ("cancel", 0, "cancelled", "cancelled"),
        ("expire", 0, "expired", "expired"),
        ("open", 0, None, "open"),
        ("reject", 0, "broker_rejected", "broker_rejected"),
        ("changed", 0, "decision_changed", "decision_changed"),
    ]
    events = []
    for name, filled, reason, _outcome in cases:
        reservation = _reservation(
            reservation_id=name,
            occurred_at="2026-07-01T09:00:00+09:00",
            limit_yen="1000",
            decision_reference="assessment",
        )
        reservation.update(quantity=200, expires_at="2026-07-10T15:30:00+09:00")
        events.append(reservation)
        # Multiple fills verify summation and the completion date.
        for i in range(filled // 100):
            events.append(
                dict(
                    event_id=f"fill-{name}-{i}",
                    type="execution",
                    occurred_at=f"2026-07-0{2 + i}T09:00:00+09:00",
                    execution_id=f"exec-{name}-{i}",
                    reservation_id=name,
                    ticker="2331",
                    side="buy",
                    quantity=100,
                    price_yen="1000",
                )
            )
        if reason is not None:
            events.append(
                dict(
                    event_id=f"release-{name}",
                    type="release",
                    occurred_at="2026-07-10T15:30:00+09:00"
                    if reason == "expired"
                    else "2026-07-09T15:30:00+09:00",
                    reservation_id=name,
                    reason=reason,
                )
            )
    events.sort(key=lambda event: event["occurred_at"])
    _ledger(app_db, events)
    _market(market, [("2026-07-02", 990.0, 1000.0), *_post_expiry_sessions(high_close=1250.0)])
    original = app_db.read_bytes(), market.read_bytes()
    payload = build_limit_outcomes(app_db=app_db, market_db=market, asof=date(2026, 8, 1))
    state = replay_events_through(
        LedgerStoreService(app_db).load().events,
        datetime.fromisoformat("2026-08-01T23:59:59+09:00"),
    )
    by_id = {item["reservation_id"]: item for item in payload["orders"]}
    for name, filled, reason, outcome in cases:
        item = by_id[name]
        assert item["outcome"] == outcome
        assert item["release_reason"] == reason
        assert item["filled_quantity"] == filled
        assert item["filled_quantity_pct"] == filled / 2
        active = state.active_reservations.get(name)
        assert item["remaining_quantity"] == (active.remaining_quantity if active else 0)
        assert item["unfilled_quantity"] == 200 - filled
        assert (item["forgone_pct"] is not None) == (reason == "expired")
    assert by_id["full"]["window_end"] == "2026-07-03"
    summary = payload["summary"]["decision_bound_orders"]
    assert summary["orders"] == 9
    assert summary["filled"] == 1
    assert summary["expired"] == summary["cancelled"] == 2
    assert summary["broker_rejected"] == summary["decision_changed"] == 1
    assert summary["still_open"] == 2
    assert summary["partially_filled_open"] == 1
    assert summary["decided_orders"] == 3
    assert summary["full_fill_rate_pct"] == 33.3
    assert summary["forgone_measured_orders"] == 2
    assert "chase_policy_decision" not in payload
    assert (app_db.read_bytes(), market.read_bytes()) == original


def _expired_case(tmp_path):
    app, market = tmp_path / "app.sqlite", tmp_path / "market.sqlite"
    _ledger(
        app,
        [
            _reservation(
                reservation_id="expired",
                occurred_at="2026-07-01T09:00:00+09:00",
                limit_yen="1000",
                decision_reference="retained-reference",
            ),
            {
                "event_id": "release",
                "type": "release",
                "occurred_at": "2026-07-10T15:30:00+09:00",
                "reservation_id": "expired",
                "reason": "expired",
            },
        ],
    )
    _market(market, [("2026-07-02", 1050.0, 1100.0), *_post_expiry_sessions(high_close=1250)])
    return app, market


@pytest.mark.parametrize("factor", [0.5, None])
def test_changed_or_unknown_price_basis_keeps_order_facts_without_price_comparison(
    tmp_path, factor
):
    app, market = _expired_case(tmp_path)
    with closing(open_connection(market)) as connection, connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET adjustment_factor=? WHERE traded_at='2026-07-13'",
            (factor,),
        )
    payload = build_limit_outcomes(app_db=app, market_db=market, asof=date(2026, 8, 1))
    order = payload["orders"][0]
    assert order["outcome"] == "expired"
    assert order["post_expiry_sessions_observed"] == POST_EXPIRY_SESSIONS
    assert order["price_comparison_unavailable_reason"] == "price_basis_unconfirmed"
    assert order["distance_to_limit_pct"] is None
    assert order["forgone_pct"] is None
    assert payload["summary"]["all_ledger_orders"]["price_incomparable_orders"] == 1
    assert payload["summary"]["all_ledger_orders"]["forgone_pending_window_orders"] == 0


def test_cutoff_and_placed_day_use_tokyo_even_for_utc_event(tmp_path):
    app, market = tmp_path / "app.sqlite", tmp_path / "market.sqlite"
    _ledger(
        app,
        [
            _reservation(
                reservation_id="utc",
                occurred_at="2026-07-30T23:30:00Z",
                limit_yen="1000",
                decision_reference=None,
            )
        ],
    )
    _market(market, [("2026-07-31", 1000.0, 1100.0)])
    before = build_limit_outcomes(app_db=app, market_db=market, asof=date(2026, 7, 30))
    assert before["orders"] == []
    after = build_limit_outcomes(app_db=app, market_db=market, asof=ASOF)
    assert after["orders"][0]["placed_on"] == "2026-07-31"


def test_sell_reservations_are_not_counted_as_buy_limit_outcomes(tmp_path):
    app, market = tmp_path / "app.sqlite", tmp_path / "market.sqlite"
    buy = _reservation(
        reservation_id="buy",
        occurred_at="2026-07-01T09:00:00+09:00",
        limit_yen="1000",
        decision_reference="buy-ref",
    )
    sell = _reservation(
        reservation_id="sell",
        occurred_at="2026-07-03T09:00:00+09:00",
        limit_yen="1200",
        decision_reference=None,
    )
    sell["side"] = "sell"
    _ledger(
        app,
        [
            buy,
            {
                "event_id": "fill-buy",
                "type": "execution",
                "occurred_at": "2026-07-02T09:00:00+09:00",
                "execution_id": "exec-buy",
                "reservation_id": "buy",
                "ticker": "2331",
                "side": "buy",
                "quantity": 100,
                "price_yen": "1000",
            },
            sell,
        ],
    )
    _market(market, [("2026-07-02", 990.0, 1000.0)])
    payload = build_limit_outcomes(app_db=app, market_db=market, asof=ASOF)
    assert payload["scope"] == "buy_reservations"
    assert [item["reservation_id"] for item in payload["orders"]] == ["buy"]


@pytest.mark.parametrize("released_at", ["2026-07-10T08:30:00+09:00", "2026-07-09T23:30:00Z"])
def test_expired_window_uses_the_same_tokyo_date_for_any_offset(tmp_path, released_at):
    app, market = tmp_path / "app.sqlite", tmp_path / "market.sqlite"
    reservation = _reservation(
        reservation_id="expired-offset",
        occurred_at="2026-07-01T09:00:00+09:00",
        limit_yen="1000",
        decision_reference=None,
    )
    reservation["expires_at"] = "2026-07-10T08:30:00+09:00"
    _ledger(
        app,
        [
            reservation,
            {
                "event_id": "release-offset",
                "type": "release",
                "occurred_at": released_at,
                "reservation_id": "expired-offset",
                "reason": "expired",
            },
        ],
    )
    _market(market, [("2026-07-09", 1100.0, 1110.0), ("2026-07-10", 1050.0, 1060.0)])
    order = build_limit_outcomes(app_db=app, market_db=market, asof=ASOF)["orders"][0]
    assert order["window_end"] == "2026-07-10"
    assert order["window_low_yen"] == 1050.0
    assert order["post_expiry_sessions_observed"] == 0


@pytest.mark.parametrize("target", ["ledger", "market"])
@pytest.mark.parametrize("alias", ["direct", "symlink", "hardlink"])
def test_cli_never_overwrites_an_input_store(tmp_path: Path, target: str, alias: str) -> None:
    app_db, market = tmp_path / "app.sqlite", tmp_path / "market.sqlite"
    _ledger(app_db, [])
    _market(market, [("2026-07-02", 990.0, 1000.0)])
    source = app_db if target == "ledger" else market
    out = source
    if alias != "direct":
        out = tmp_path / "report.yaml"
        if alias == "symlink":
            out.symlink_to(source)
        else:
            out.hardlink_to(source)
    original = app_db.read_bytes(), market.read_bytes()

    assert (
        main(
            [
                "--db",
                str(app_db),
                "--sqlite-path",
                str(market),
                "--asof",
                ASOF.isoformat(),
                "--out",
                str(out),
            ]
        )
        == 1
    )
    assert (app_db.read_bytes(), market.read_bytes()) == original
