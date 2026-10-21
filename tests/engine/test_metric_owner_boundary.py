"""財務計算の保存型・純粋依存と共有split算術の境界。"""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import closing
from datetime import date

import pytest
from pydantic import TypeAdapter

from baibai_engine.market_context.prices import read_price_bars
from baibai_engine.storage.observations.schema import open_connection
from baibai_engine.valuation.metrics.models import FinancialSnapshot


def test_financial_snapshot_json_preserves_values_and_missingness() -> None:
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
    assert payload["latest_financial_disclosure_date"] == "2026-07-01"
    assert payload["per_forward"] == 10.0
    assert payload["per_trailing"] is None
    assert payload["pbr"] == 0.0
    assert payload["p_s"] == -1.0
    assert payload["eps"] == 0.0
    assert payload["sales_ttm"] == 0.0
    assert payload["ocf_ttm"] is None
    assert payload["earnings_forecast_source"] is None
    assert payload["dividend_forecast_source"] is None
    restored = TypeAdapter(FinancialSnapshot).validate_json(json.dumps(payload))
    assert restored == snapshot


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
    with closing(open_connection(path)) as connection, connection:
        connection.executemany(
            "INSERT INTO jquants_daily_bars "
            "(ticker, traded_at, close, adjustment_factor, turnover_value) VALUES (?, ?, ?, ?, ?)",
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
