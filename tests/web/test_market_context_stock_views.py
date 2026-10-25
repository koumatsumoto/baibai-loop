"""既存routeとexportは同じdomain出力・日付・nullを渡す。"""

import hashlib
import json
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from tests.helpers.master import insert_master_rows
from tests.helpers.screening_run import screening_run_payload, stock_analysis
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.machine_analysis.market_context import security as security_context
from baibai_engine.machine_analysis.screening.rule_config import load_screening_rules
from baibai_engine.machine_analysis.screening.run_store import ScreeningRunStore
from baibai_engine.read_api.market_context import (
    build_market_snapshot,
    build_security_market_context,
)
from baibai_engine.read_api.company_valuation import VALUATION_HISTORY_SESSIONS
from baibai_engine.storage.observations.schema import open_connection
from baibai_web.api.server import create_app
from baibai_web.materialize import export_read_models
from baibai_web.sources.db_sources import DbMarketPriceSource


def _seed_market(root: Path) -> Path:
    path = root / "stores/market/market.sqlite"
    for ticker in ("1321", "2331", "2332"):
        insert_daily_bars_from_closes(
            path, ticker, [100 + index for index in range(120)], end_date=date(2026, 7, 27)
        )
    with closing(open_connection(path)) as connection:
        insert_master_rows(
            connection,
            [
                ("2026-07-26", ticker, ticker, "プライム", "サービス業", "011")
                for ticker in ("2331", "2332")
            ],
        )
        connection.commit()
    return path


def test_market_route_keeps_requested_date_and_native_snapshot(app_method_root: Path) -> None:
    path = _seed_market(app_method_root)
    expected = build_market_snapshot(sqlite_path=path, asof_date=date(2026, 7, 28))
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/market-context", params={"as_of": "2026-07-28"})
    assert response.status_code == 200
    snapshot = response.json()["market_snapshot"]
    assert snapshot.pop("return_basis") == "price_return_excluding_cash_dividends"
    assert snapshot == expected


def test_security_route_and_export_preserve_context_and_store_bytes(
    app_method_root: Path,
    tmp_path: Path,
) -> None:
    market = _seed_market(app_method_root)
    app = app_method_root / "stores/application/baibai.sqlite"
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (market, app)}
    with TestClient(create_app(app_method_root), base_url="http://127.0.0.1") as client:
        response = client.get("/api/securities/2331")
    assert response.status_code == 200
    context = response.json()["market_context"]
    expected = build_security_market_context(
        sqlite_path=market,
        ticker="2331",
        asof_date=date.fromisoformat(context["as_of"]),
        self_range_window_bars=VALUATION_HISTORY_SESSIONS,
    )
    assert "bollinger_20bar" not in expected["price"]
    assert "bollinger_20bar" not in context["price"]
    assert context == {
        "as_of": context["as_of"],
        **expected,
        "return_basis": "price_return_excluding_cash_dividends",
    }
    output = tmp_path / "serving"
    export_read_models(app_method_root, output)
    exported = json.loads((output / "views/security--2331.json").read_text())
    assert exported["market_context"] == context
    snapshot = json.loads((output / "views/market-context.json").read_text())["market_snapshot"]
    assert snapshot["as_of"] == context["as_of"]
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before} == before


def test_unwritten_store_does_not_become_zero_context(tmp_path: Path) -> None:
    source = DbMarketPriceSource(tmp_path / "absent.sqlite")
    assert source.market_snapshot(as_of=date(2026, 7, 28)) is None
    assert source.security_market_context("2331", as_of=date(2026, 7, 28)) is None


def test_export_batches_keep_all_json_and_reread_prices_next_export(
    app_method_root: Path, tmp_path: Path, mocker
) -> None:
    now = datetime(2026, 8, 1, 12, tzinfo=ZoneInfo("Asia/Tokyo"))
    for module in (
        "baibai_web.materialize",
        "baibai_web.sources.db_sources",
        "baibai_web.readmodel.stocks.detail",
        "baibai_web.readmodel.stocks.screening",
        "baibai_web.readmodel.builders.dashboard",
        "baibai_web.readmodel.builders.tasks",
        "baibai_web.readmodel.builders.daily_delta",
        "baibai_web.readmodel.builders.meta",
    ):
        mocker.patch(f"{module}.datetime", wraps=datetime).now.return_value = now
    market = _seed_market(app_method_root)
    # ledger fixtureの未解消予約8929もexport対象なので、合計65銘柄に揃える。
    tickers = [
        "0001",
        "0002",
        "2331",
        "2332",
        *[str(4000 + index) for index in range(60)],
        "8929",
    ]
    for ticker in tickers[4:]:
        insert_daily_bars_from_closes(
            market,
            ticker,
            [100 + index for index in range(25)],
            end_date=now.date() - timedelta(days=500) if ticker == "4000" else date(2026, 7, 27),
        )
    with closing(open_connection(market)) as connection, connection:
        insert_master_rows(
            connection,
            [
                (
                    "2026-07-26",
                    ticker,
                    ticker,
                    "プライム",
                    "機械" if index % 2 else "サービス業",
                    "011",
                )
                for index, ticker in enumerate(tickers)
            ],
        )
        connection.executemany(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close,adjustment_factor) "
            "VALUES (?, ?, ?, ?)",
            [
                ("0002", "2026-07-28", None, 0.5),
                ("2331", "2026-07-28", None, 0.5),
                ("2331", "2026-08-02", 1000, 0.1),  # beyond the requested cutoff
            ],
        )
        connection.execute(
            "DELETE FROM jquants_daily_bars WHERE ticker='2332' AND traded_at='2026-07-07'"
        )
    ScreeningRunStore(app_method_root / "stores/screening/runs.sqlite").publish_run(
        screening_run_payload(
            as_of="2026-08-01",
            universe_size=len(tickers),
            stock_analyses=[stock_analysis(ticker) for ticker in tickers],
        ),
        rules=load_screening_rules(),
    )
    scalar = mocker.patch.object(DbMarketPriceSource, "prepare_security_market_contexts")
    first = tmp_path / "scalar"
    export_read_models(app_method_root, first)
    mocker.stop(scalar)
    read = mocker.spy(security_context, "read_price_bars")
    prepare = mocker.spy(DbMarketPriceSource, "prepare_security_market_contexts")
    second = tmp_path / "batched"
    export_read_models(app_method_root, second)
    expected = {
        path.relative_to(first): json.loads(path.read_text()) for path in first.rglob("*.json")
    }
    actual = {
        path.relative_to(second): json.loads(path.read_text()) for path in second.rglob("*.json")
    }
    assert actual == expected
    assert len(list((second / "views").glob("security--*.json"))) == 65
    context = actual[Path("views/security--2331.json")]["market_context"]
    assert context["price"]["close"] == 109.5
    assert context["price"]["resolved_date"] == "2026-07-27"
    for ticker in ("0001", "0002"):
        missing = actual[Path(f"views/security--{ticker}.json")]["market_context"]
        assert missing["price"] is None
        assert missing["relative"] is None
    assert [call.args[1] for call in prepare.call_args_list] == [tickers[:64], tickers[64:]]
    source = prepare.call_args.args[0]
    assert set(source._security_contexts) == {(now.date(), tickers[-1])}
    long_reads = [
        call.kwargs["tickers"]
        for call in read.call_args_list
        if call.kwargs["start"] == now.date() - timedelta(days=1130)
    ]
    assert long_reads == [tuple(tickers[:64]), tuple(tickers[64:])]
    with closing(open_connection(market)) as connection, connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET close=220 WHERE ticker='2331' AND traded_at='2026-07-27'"
        )
    third = tmp_path / "next"
    export_read_models(app_method_root, third)
    refreshed = json.loads((third / "views/security--2331.json").read_text())
    assert refreshed["market_context"]["price"]["close"] == 110
    assert prepare.call_count == 4
    assert prepare.call_args.args[0] is not source
