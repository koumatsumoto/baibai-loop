from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest
import yaml

from baibai_loop.market.sqlite import open_connection, store_jquants_market_calendar
from baibai_loop.position.cli import main
from baibai_loop.position.ledger import load_portfolio_ledger, reconcile_portfolio

FIXTURE = Path(__file__).parent / "fixtures" / "portfolio-ledger" / "representative.yaml"
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
    return main(
        [
            "market-price-draft",
            "--root",
            str(root),
            "--ledger",
            "ledger.yaml",
            "--sqlite",
            "data/screening/market.sqlite",
            "--asof",
            ASOF.isoformat(),
            "--out",
            out,
        ]
    )


def test_market_price_draft_replaces_all_holdings_with_exact_raw_close(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ledger.yaml"
    _two_holding_ledger(ledger)
    source_bytes = ledger.read_bytes()
    sqlite_path = tmp_path / "data/screening/market.sqlite"
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
        "source_ledger",
        "source_ledger_sha256",
        "market_data_fingerprint",
        "draft_sha256",
        "output",
    }
    assert stdout["source_ledger"] == str(ledger)
    assert stdout["source_ledger_sha256"] == hashlib.sha256(source_bytes).hexdigest()
    assert len(stdout["market_data_fingerprint"]) == 64
    assert (
        stdout["draft_sha256"]
        == hashlib.sha256((tmp_path / "drafts/market-price.yaml").read_bytes()).hexdigest()
    )
    assert stdout["output"] == str(tmp_path / "drafts/market-price.yaml")
    assert ledger.read_bytes() == source_bytes
    draft = load_portfolio_ledger(tmp_path / "drafts/market-price.yaml")
    snapshot = reconcile_portfolio(draft)
    assert {holding.ticker for holding in snapshot.holdings} == {"2331", "2749"}
    assert {str(price.price_yen) for price in draft.market_prices} == {"1200.0", "1010.0"}
    assert {price.observed_at.date() for price in draft.market_prices} == {ASOF}
    assert {price.source_kind for price in draft.market_prices} == {"licensed_dataset"}
    assert {price.price_basis for price in draft.market_prices} == {"unadjusted_close"}
    assert draft.as_of.isoformat() == "2026-07-13T15:30:00+09:00"
    assert {price.source_ref for price in draft.market_prices} == {
        f"data/screening/market.sqlite:jquants_daily_bars:2331:{ASOF}",
        f"data/screening/market.sqlite:jquants_daily_bars:2749:{ASOF}",
    }


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
    _seed_market(tmp_path / "data/screening/market.sqlite", rows)

    assert _run(tmp_path) == 2
    assert "raw close" in capsys.readouterr().err
    assert not (tmp_path / "drafts/market-price.yaml").exists()


def test_market_price_draft_fails_when_calendar_coverage_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _two_holding_ledger(tmp_path / "ledger.yaml")
    sqlite_path = tmp_path / "data/screening/market.sqlite"
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
    rollback_day = date(2026, 7, 10)

    assert (
        main(
            [
                "market-price-draft",
                "--root",
                str(tmp_path),
                "--ledger",
                "ledger.yaml",
                "--sqlite",
                "data/screening/market.sqlite",
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
        tmp_path / "data/screening/market.sqlite",
        [("2331", 1200.0, 1200.0, 1.0), ("2749", 1010.0, 1010.0, 1.0)],
    )
    if out == "drafts/existing.yaml":
        existing = tmp_path / out
        existing.parent.mkdir(parents=True)
        existing.write_text("existing", encoding="utf-8")

    assert _run(tmp_path, out=out) == 2
    assert capsys.readouterr().err
