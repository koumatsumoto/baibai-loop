from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from tests.helpers.db_seed import seed_ledger
from tools.experiments.measure_limit_outcomes import (
    POST_EXPIRY_SESSIONS,
    LimitOutcomeMeasurementError,
    build_limit_outcomes,
    main,
)

from baibai_engine.market.sqlite import open_connection
from baibai_engine.position.ledger import PortfolioLedgerDocument

ASOF = date(2026, 7, 31)


def _ledger(path: Path, events: list[dict[str, object]]) -> None:
    document = PortfolioLedgerDocument.model_validate(
        {
            "schema_version": 2,
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
            "market_prices": [],
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
    assert summary["all_ledger_orders"]["fill_rate_pct"] == pytest.approx(50.0)
    assert summary["decision_bound_orders"]["orders"] == 1
    assert summary["decision_bound_orders"]["fill_rate_pct"] == pytest.approx(0.0)
    # 判断経路を通った注文が 1 件では chase policy を動かさない。
    assert payload["chase_policy_decision"]["ready"] is False


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
