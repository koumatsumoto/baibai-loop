from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger

from baibai_engine.market.sqlite import open_connection, store_jquants_market_calendar
from baibai_engine.position.cli import main
from baibai_engine.position.drafts import load_draft
from baibai_engine.position.ledger import (
    PortfolioLedgerError,
    reconcile_portfolio,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "portfolio-ledger" / "representative.yaml"
ASOF = date(2026, 7, 13)


def _two_holding_ledger(path: Path) -> None:
    payload = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    insert_at = next(
        index
        for index, event in enumerate(payload["events"])
        if event["event_id"] == "reserve-8929-pending"
    )
    payload["events"][insert_at:insert_at] = [
        {
            "event_id": "reserve-2749-test",
            "type": "reservation",
            "occurred_at": "2026-07-02T10:00:00+09:00",
            "reservation_id": "reservation-2749-test",
            "order_id": "order-2749-test",
            "ticker": "2749",
            "sector": "サービス業",
            "common_factors": ["domestic-demand"],
            "quantity": 100,
            "price_guard_yen": 1000,
            "expires_at": "2026-07-10T15:30:00+09:00",
        },
        {
            "event_id": "fill-2749-test",
            "type": "execution",
            "occurred_at": "2026-07-02T10:01:00+09:00",
            "execution_id": "execution-2749-test",
            "reservation_id": "reservation-2749-test",
            "ticker": "2749",
            "side": "buy",
            "quantity": 100,
            "price_yen": 990,
        },
    ]
    payload["market_prices"].append(
        {
            "ticker": "2749",
            "price_yen": 995,
            "observed_at": "2026-07-11T10:00:00+09:00",
            "source_kind": "test_fixture",
            "price_basis": "close",
            "source_ref": "offline-fixture:2749",
        }
    )
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _seed_market(
    path: Path,
    rows: list[tuple[str, float | None, float | None, float | None]],
) -> None:
    store_jquants_market_calendar(
        path,
        [{"Date": ASOF.isoformat(), "HolidayDivision": "1"}],
        requested_start=ASOF,
        requested_end=ASOF,
    )
    conn = open_connection(path)
    try:
        conn.executemany(
            "INSERT INTO jquants_daily_bars"
            "(ticker, traded_at, close, adjustment_close, adjustment_factor) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (ticker, ASOF.isoformat(), close, adjusted, factor)
                for ticker, close, adjusted, factor in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _run(root: Path, *, out: str = "drafts/market-price.yaml") -> int:
    _import_ledger(root / "ledger.yaml", root / "app.sqlite")
    return main(
        [
            "market-price-draft",
            "--root",
            str(root),
            "--db",
            str(root / "app.sqlite"),
            "--sqlite",
            "stores/market/market.sqlite",
            "--asof",
            ASOF.isoformat(),
            "--out",
            out,
        ]
    )


def _import_ledger(ledger_path: Path, db_path: Path) -> None:
    source = load_portfolio_ledger(ledger_path)
    canonical = source.model_copy(
        update={
            "market_prices": tuple(
                price.model_copy(update={"source_kind": "licensed_dataset"})
                for price in source.market_prices
            )
        }
    )
    seed_ledger(db_path, canonical)


def test_market_price_draft_replaces_all_holdings_with_exact_raw_close(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ledger.yaml"
    _two_holding_ledger(ledger)
    sqlite_path = tmp_path / "stores/market/market.sqlite"
    _seed_market(
        sqlite_path,
        [
            ("2331", 1200.0, 600.0, 0.5),
            ("2749", 1010.0, 1010.0, 1.0),
        ],
    )

    assert _run(tmp_path) == 0

    stdout = yaml.safe_load(capsys.readouterr().out)
    assert set(stdout) == {
        "source_append_head",
        "market_data_fingerprint",
        "draft_sha256",
        "output",
    }
    assert stdout["source_append_head"] > 0
    assert len(stdout["market_data_fingerprint"]) == 64
    assert len(stdout["draft_sha256"]) == 64
    assert stdout["output"] == str(tmp_path / "drafts/market-price.yaml")
    draft = load_draft(tmp_path / "drafts/market-price.yaml").replacement
    snapshot = reconcile_portfolio(draft)
    assert {holding.ticker for holding in snapshot.holdings} == {"2331", "2749"}
    assert {str(price.price_yen) for price in draft.market_prices} == {"1200.0", "1010.0"}
    assert {price.observed_at.date() for price in draft.market_prices} == {ASOF}
    assert {price.source_kind for price in draft.market_prices} == {"licensed_dataset"}
    assert {price.price_basis for price in draft.market_prices} == {"unadjusted_close"}
    assert draft.as_of.isoformat() == "2026-07-13T15:30:00+09:00"
    assert {price.source_ref for price in draft.market_prices} == {
        f"stores/market/market.sqlite:jquants_daily_bars:2331:{ASOF}",
        f"stores/market/market.sqlite:jquants_daily_bars:2749:{ASOF}",
    }


def test_market_price_draft_prices_new_fill_missing_from_input_market_prices(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "ledger.yaml"
    payload = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    payload["as_of"] = "2026-07-14T09:00:00+09:00"
    payload["events"].extend(
        [
            {
                "event_id": "reserve-2749-new-fill",
                "type": "reservation",
                "occurred_at": "2026-07-14T08:59:00+09:00",
                "reservation_id": "reservation-2749-new-fill",
                "order_id": "order-2749-new-fill",
                "ticker": "2749",
                "sector": "サービス業",
                "common_factors": ["domestic-demand"],
                "quantity": 100,
                "price_guard_yen": 1000,
                "expires_at": "2026-07-15T15:30:00+09:00",
            },
            {
                "event_id": "fill-2749-new-fill",
                "type": "execution",
                "occurred_at": "2026-07-14T09:00:00+09:00",
                "execution_id": "execution-2749-new-fill",
                "reservation_id": "reservation-2749-new-fill",
                "ticker": "2749",
                "side": "buy",
                "quantity": 100,
                "price_yen": 990,
            },
        ]
    )
    ledger_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    assert all(price["ticker"] != "2749" for price in payload["market_prices"])
    document = load_portfolio_ledger(ledger_path)
    with pytest.raises(PortfolioLedgerError, match="market price is required for holding 2749"):
        reconcile_portfolio(document)

    _seed_market(
        tmp_path / "stores/market/market.sqlite",
        [("2331", 1200.0, 1200.0, 1.0), ("2749", 1010.0, 1010.0, 1.0)],
    )

    assert _run(tmp_path) == 0
    capsys.readouterr()
    draft = load_draft(tmp_path / "drafts/market-price.yaml").replacement
    snapshot = reconcile_portfolio(draft)
    assert {holding.ticker for holding in snapshot.holdings} == {"2331", "2749"}
    assert {price.ticker for price in draft.market_prices} == {"2331", "2749"}
    assert draft.as_of.isoformat() == "2026-07-14T09:00:00+09:00"
    new_fill_price = next(price for price in draft.market_prices if price.ticker == "2749")
    assert new_fill_price.observed_at.isoformat() == "2026-07-13T15:30:00+09:00"
    assert new_fill_price.observed_at < draft.as_of


@pytest.mark.parametrize(
    "rows",
    [
        [("2331", 1200.0, 1200.0, 1.0)],
        [("2331", None, 600.0, 0.5), ("2749", 1010.0, 1010.0, 1.0)],
    ],
)
def test_market_price_draft_fails_when_any_exact_raw_close_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    rows: list[tuple[str, float | None, float | None, float | None]],
) -> None:
    _two_holding_ledger(tmp_path / "ledger.yaml")
    _seed_market(tmp_path / "stores/market/market.sqlite", rows)

    assert _run(tmp_path) == 2
    assert "raw close" in capsys.readouterr().err
    assert not (tmp_path / "drafts/market-price.yaml").exists()


def test_market_price_draft_fails_when_calendar_coverage_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_holding_ledger(tmp_path / "ledger.yaml")
    sqlite_path = tmp_path / "stores/market/market.sqlite"
    conn = open_connection(sqlite_path)
    try:
        conn.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
            [("2331", ASOF.isoformat(), 1200.0), ("2749", ASOF.isoformat(), 1010.0)],
        )
        conn.commit()
    finally:
        conn.close()

    assert _run(tmp_path) == 2
    assert "calendar coverage" in capsys.readouterr().err
    assert not (tmp_path / "drafts/market-price.yaml").exists()


def test_market_price_draft_rejects_price_date_rollback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_holding_ledger(tmp_path / "ledger.yaml")
    _import_ledger(tmp_path / "ledger.yaml", tmp_path / "app.sqlite")
    rollback_day = date(2026, 7, 10)

    assert (
        main(
            [
                "market-price-draft",
                "--root",
                str(tmp_path),
                "--db",
                str(tmp_path / "app.sqlite"),
                "--sqlite",
                "stores/market/market.sqlite",
                "--asof",
                rollback_day.isoformat(),
                "--out",
                "drafts/rollback.yaml",
            ]
        )
        == 2
    )
    assert "roll back current holding market prices" in capsys.readouterr().err
    assert not (tmp_path / "drafts/rollback.yaml").exists()


@pytest.mark.parametrize("out", ["ledger.yaml", "../outside.yaml", "drafts/existing.yaml"])
def test_market_price_draft_rejects_unsafe_or_existing_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], out: str
) -> None:
    _two_holding_ledger(tmp_path / "ledger.yaml")
    _seed_market(
        tmp_path / "stores/market/market.sqlite",
        [("2331", 1200.0, 1200.0, 1.0), ("2749", 1010.0, 1010.0, 1.0)],
    )
    if out == "drafts/existing.yaml":
        existing = tmp_path / out
        existing.parent.mkdir(parents=True)
        existing.write_text("existing", encoding="utf-8")

    assert _run(tmp_path, out=out) == 2
    assert capsys.readouterr().err
