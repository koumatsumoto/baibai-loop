from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from baibai_engine.screening.discovery.review_set import ReviewSetContractError, build_review_set
from baibai_engine.screening.rule_config import load_screening_rules
from baibai_engine.screening.run_store import (
    ScreeningRunReader,
    ScreeningRunStore,
    initialize_run_store,
)
from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION


def _analysis(ticker: str = "1301") -> dict[str, object]:
    return {
        "ticker": ticker,
        "name": ticker,
        "sector_33": "情報・通信業",
        "market_cap_oku": 500,
        "avg_turnover_oku": 5.0,
        "listing_span_days": 1000,
        "jpx_flags": [],
        "per_forward": 10.0,
        "per_trailing": 11.0,
        "pbr": 0.8,
        "p_s": 1.0,
        "ev_ebitda": 5.0,
        "pcfr": 8.0,
        "metrics": {
            "per_forward_sector_gap": -0.5,
            "normalized_per_3fy": 10.0,
            "fcf_yield": 0.08,
            "ocf_yield": 0.1,
            "asset_backed_ratio": 0.5,
            "net_cash_to_market_cap": 0.25,
            "pbr_sector_gap": -0.3,
            "equity_ratio": 0.6,
            "p_s_sector_gap": -0.4,
            "sales_yoy": 0.05,
            "operating_profit": 12.0,
            "sales_ttm": 100.0,
            "total_assets": 200.0,
            "debt": 20.0,
            "cash": 30.0,
            "er_annual": 0.13,
        },
    }


def _run() -> dict[str, object]:
    return {
        "run_id": "screening-20260708",
        "run_date": "2026-07-08",
        "asof_date": "2026-07-08",
        "run_at": "2026-07-08T18:00:00+09:00",
        "universe_size": 1,
        "screening_rules_hash": "a" * 64,
        "er_model_version": "expected-return-v1",
        "security_analyses": [_analysis()],
    }


def test_current_schema_is_created_once(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    assert initialize_run_store(database) == RUN_STORE_SCHEMA_VERSION == 5
    assert initialize_run_store(database) == 5
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
    assert {"screening_run", "security_analysis", "review_set"} <= tables


def test_obsolete_cache_is_rejected_instead_of_migrated(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE old_cache (value TEXT)")
        connection.execute("PRAGMA user_version = 4")
    with pytest.raises(RuntimeError, match="rebuild it"):
        initialize_run_store(database)


def test_run_and_review_set_round_trip(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    screening_rules = load_screening_rules()
    rules = screening_rules.candidate_discovery
    required_jpx_flags = screening_rules.universe.required_jpx_flags
    store = ScreeningRunStore(database)
    store.publish_run(_run(), run_revision_id="run-a")
    payload = build_review_set(
        [_analysis()],
        rules=rules,
        required_jpx_flags=required_jpx_flags,
    )
    payload.update(
        {
            "review_set_id": "review-set-a",
            "run_revision_id": "run-a",
            "as_of": "2026-07-08",
            "created_at": "2026-07-08T18:30:00+09:00",
            "screening_rules_hash": "a" * 64,
        }
    )
    result = store.publish_review_set(
        run_revision_id="run-a",
        payload=payload,
        rules=rules,
        required_jpx_flags=required_jpx_flags,
        review_set_id="review-set-a",
    )
    assert result.inserted
    reader = ScreeningRunReader(database)
    assert reader.get_run("run-a").security_analyses[0]["ticker"] == "1301"  # type: ignore[union-attr]
    assert reader.get_review_set("review-set-a").payload["entries"][0]["ticker"] == "1301"  # type: ignore[union-attr,index]

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE review_set SET payload = json_set("
            "payload, '$.entries[0].analysis.identity_liquidity.market_cap_oku', '500')"
        )
    with pytest.raises(ReviewSetContractError, match="published review set is invalid"):
        reader.get_review_set("review-set-a")
