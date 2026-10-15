"""財務計算の保存型・純粋依存と共有split算術の境界。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import date
from hashlib import sha256

import pytest
from pydantic import TypeAdapter

from baibai_engine.market_context.prices import read_price_bars
from baibai_engine.valuation.metrics.models import FinancialSnapshot


def test_financial_snapshot_keeps_pre_move_default_json() -> None:
    snapshot = FinancialSnapshot(
        latest_financial_disclosure_date=date(2026, 7, 1),
        per_forward=10.0,
        per_trailing=None,
        pbr=0.0,
        ev_ebitda=None,
        p_s=-1.0,
        pcfr=None,
        eps=0.0,
        sales_ttm=0.0,
        ocf_ttm=None,
    )
    payload = TypeAdapter(FinancialSnapshot).dump_python(snapshot, mode="json")
    # v27 adds optional source traces; the previous fields/defaults stay identical.
    assert payload.pop("earnings_forecast_source") is None
    assert payload.pop("dividend_forecast_source") is None
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # 同じ入力からmain 537477c2の元schemaで取得した全field/defaultのJSON。
    assert sha256(encoded.encode()).hexdigest() == (
        "47e0cbc2f820da7e276b0c9aa034184a914a8cf32053716c80e8a0f00aae61c9"
    )


@pytest.mark.parametrize(
    "module",
    [
        "market_context.history",
        "market_context.margin",
    ],
)
def test_market_context_calculation_cold_import_does_not_load_composition_or_io(module) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib,json,sys; importlib.import_module(sys.argv[1]); "
                "print(json.dumps(sorted(sys.modules)))"
            ),
            f"baibai_engine.{module}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    modules = json.loads(result.stdout)
    forbidden = (
        "baibai_engine.screening",
        "baibai_engine.research",
        "baibai_engine.read_api",
        "baibai_engine.storage",
        "baibai_engine.market.providers",
        "baibai_engine.market.store",
        "baibai_engine.market_context.prices",
    )
    assert not [name for name in modules if name.startswith(forbidden)]


def test_price_reader_keeps_nontraded_action_without_inventing_a_price(tmp_path) -> None:
    path = tmp_path / "market.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE jquants_daily_bars "
            "(ticker TEXT, traded_at TEXT, close REAL, adjustment_factor REAL, turnover_value REAL)"
        )
        connection.executemany(
            "INSERT INTO jquants_daily_bars VALUES (?, ?, ?, ?, ?)",
            [
                ("130A", "2026-07-10", 100.0, 1.0, 1000.0),
                ("130A", "2026-07-11", None, 0.5, None),
                ("130A", "2026-07-12", 80.0, 1.0, 2000.0),
            ],
        )
    before = path.read_bytes()
    bars = read_price_bars(path, start=date(2026, 7, 10), end=date(2026, 7, 12))["130A"]
    assert [(bar.traded_at, bar.price) for bar in bars] == [
        (date(2026, 7, 10), 50.0),
        (date(2026, 7, 12), 80.0),
    ]
    assert [bar.turnover_value for bar in bars] == [1000.0, 2000.0]
    assert path.read_bytes() == before
