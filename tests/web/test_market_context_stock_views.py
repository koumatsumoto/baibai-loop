"""既存routeとexportは同じdomain出力・日付・nullを渡す。"""

import hashlib
import json
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.read_api.market_context import (
    build_market_snapshot,
    build_security_market_context,
)
from baibai_engine.read_api.valuation import VALUATION_HISTORY_SESSIONS
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
    with open_connection(path) as connection:
        connection.executemany(
            "INSERT INTO jquants_master_snapshots "
            "(snapshot_date,ticker,name,market,sector_33,is_common_stock) "
            "VALUES ('2026-07-26',?,?,'プライム','サービス業',1)",
            [(ticker, ticker) for ticker in ("2331", "2332")],
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
