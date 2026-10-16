"""保存機械値・候補外・profile公開fieldの読取回帰。"""

from __future__ import annotations

import hashlib
import socket
import sqlite3
from datetime import date

import pytest
from tests.engine.test_screening_run_store import _publish, _run

from baibai_engine.read_api.ticker_profile import _load_stock_analysis_context
from baibai_engine.read_api.valuation import stored_machine_valuation

ASOF = date(2026, 10, 14)


def _row(*, anchor=1600.0):
    return {
        "ticker": "1111",
        "name": "合成会社",
        "sector_33": "機械",
        "market_cap_oku": None,
        "price_history_span_days": 20,
        "jpx_flags": [],
        "metrics": {
            "er_annual": 0.075,
            "fv_sector_median_yen": anchor,
            "er_model_version": "expected-return-v1",
            "market_price_yen": 1000.0,
        },
    }


def test_candidates_do_not_authorize_or_suppress_valuation_and_profile(tmp_path) -> None:
    path = tmp_path / "runs.sqlite"
    _publish(path, _run(rows=[_row()], asof="2026-10-13"), revision="old")
    _publish(path, _run(rows=[_row()], at="2026-10-14T09:00:00+09:00"), revision="early")
    _publish(path, _run(rows=[_row(anchor=1700.0)]), revision="latest")
    _publish(path, _run(rows=[_row(anchor=2000.0)], asof="2026-10-15"), revision="future")
    value = stored_machine_valuation(path, ticker="1111", as_of=ASOF)
    assert value is not None
    assert value.valuation is not None
    assert value.run_revision_id == "latest"
    assert value.valuation.sector_median_anchor_yen == 1700.0
    assert value.valuation.er_annual == 0.075
    profile = _load_stock_analysis_context(path, "1111", ASOF, run_revision_id=None)
    assert profile["screening_run_revision_id"] == "latest"
    assert profile["has_stock_analysis"] is True
    assert profile["stock_analysis"]["candidate"]["reasons"] == []
    assert "has_security_analysis" not in profile
    assert "security_analysis" not in profile


@pytest.mark.parametrize(
    ("revision", "expected"), [("unknown", "unknown run_revision_id"), ("future", "after cutoff")]
)
def test_explicit_read_rejects_unknown_and_future_without_fallback(
    tmp_path, revision, expected
) -> None:
    path = tmp_path / "runs.sqlite"
    _publish(path, _run(rows=[_row()]), revision="current")
    _publish(path, _run(rows=[_row()], asof="2026-10-15"), revision="future")
    with pytest.raises(ValueError, match=expected):
        stored_machine_valuation(path, ticker="1111", as_of=ASOF, run_revision_id=revision)
    with pytest.raises(ValueError, match=expected):
        _load_stock_analysis_context(path, "1111", ASOF, run_revision_id=revision)


def test_missing_store_ticker_and_anchor_remain_distinct(tmp_path) -> None:
    path = tmp_path / "runs.sqlite"
    assert stored_machine_valuation(path, ticker="1111", as_of=ASOF) is None
    _publish(path, _run(rows=[_row(anchor=None)]), revision="run")
    missing = stored_machine_valuation(path, ticker="9999", as_of=ASOF)
    assert missing is not None
    assert missing.valuation is None
    profile = _load_stock_analysis_context(path, "9999", ASOF, run_revision_id=None)
    assert profile["has_stock_analysis"] is False
    assert profile["screening_run_revision_id"] == "run"
    assert "stock_analysis" not in profile
    known = stored_machine_valuation(path, ticker="1111", as_of=ASOF)
    assert known is not None
    assert known.valuation is not None
    assert known.valuation.sector_median_anchor_yen is None
    assert known.valuation.er_annual == 0.075


def test_read_does_not_network_write_or_mutate_saved_candidate_context(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "runs.sqlite"
    _publish(path, _run(rows=[_row()]), revision="run")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    original = sqlite3.connect

    def read_only(database, *args, **kwargs):
        assert "mode=ro" in database
        connection = original(database, *args, **kwargs)

        def authorize(action, *_rest):
            assert action not in {
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_CREATE_TABLE,
                sqlite3.SQLITE_DROP_TABLE,
            }
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    def forbidden(*_args, **_kwargs):
        pytest.fail("Stock Analysis read attempted network I/O")

    monkeypatch.setattr(sqlite3, "connect", read_only)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert stored_machine_valuation(path, ticker="1111", as_of=ASOF) is not None
    assert _load_stock_analysis_context(path, "1111", ASOF, run_revision_id=None)[
        "has_stock_analysis"
    ]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
