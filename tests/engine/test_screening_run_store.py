from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from baibai_engine.read_api import screening_review_set_payloads
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


def test_latest_review_set_resolves_exact_asof_by_publication_time(tmp_path: Path) -> None:
    database = tmp_path / "runs.sqlite"
    screening_rules = load_screening_rules()
    rules = screening_rules.candidate_discovery
    required_jpx_flags = screening_rules.universe.required_jpx_flags
    store = ScreeningRunStore(database)
    store.publish_run(_run(), run_revision_id="run-a")
    base = build_review_set([_analysis()], rules=rules, required_jpx_flags=required_jpx_flags)
    for review_set_id, created_at in (
        ("review-set-earlier", "2026-07-08T19:00:00+09:00"),
        ("review-set-latest", "2026-07-08T11:00:00+00:00"),
    ):
        payload = {
            **base,
            "review_set_id": review_set_id,
            "run_revision_id": "run-a",
            "as_of": "2026-07-08",
            "created_at": created_at,
            "screening_rules_hash": "a" * 64,
        }
        store.publish_review_set(
            run_revision_id="run-a",
            payload=payload,
            rules=rules,
            required_jpx_flags=required_jpx_flags,
            review_set_id=review_set_id,
        )

    reader = ScreeningRunReader(database)
    latest = reader.latest_review_set(as_of_date="2026-07-08")
    assert latest is not None
    assert latest.review_set_id == "review-set-latest"
    assert reader.latest_review_set(as_of_date="2026-07-09") is None
    expected = ["review-set-latest", "review-set-earlier"]
    assert [row.review_set_id for row in reader.list_review_sets()] == expected
    assert [row["review_set_id"] for row in screening_review_set_payloads(database)] == expected
    assert [
        row["review_set_id"]
        for row in screening_review_set_payloads(database, run_revision_id="run-a")
    ] == expected


def test_web_projection_rejects_malformed_current_review_set_like_point_read(
    tmp_path: Path,
) -> None:
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
    store.publish_review_set(
        run_revision_id="run-a",
        payload=payload,
        rules=rules,
        required_jpx_flags=required_jpx_flags,
        review_set_id="review-set-a",
    )
    assert len(screening_review_set_payloads(database, run_revision_id="run-a")) == 1
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE review_set SET payload = json_set("
            "json_remove(payload, '$.screening_rules_hash'), "
            "'$.historical_context', json('{}'))"
        )

    with pytest.raises(ReviewSetContractError, match="published review set is invalid"):
        ScreeningRunReader(database).get_review_set("review-set-a")

    with pytest.raises(ReviewSetContractError, match="published review set is invalid"):
        screening_review_set_payloads(database, run_revision_id="run-a")


@pytest.mark.parametrize("equal_instant", [False, True])
def test_run_order_prune_and_read_api_use_real_instant(tmp_path, equal_instant):
    from datetime import date

    from baibai_engine.read_api.screening import (
        previous_run_revision_id,
        screening_run_payload,
        stored_screening_rows,
    )

    database = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(database)
    store.publish_run(_run(), run_revision_id="run-a")
    new = {**_run(), "run_at": f"2026-07-08T{9 if equal_instant else 10:02}:00:00+00:00"}
    store.publish_run(new, run_revision_id="run-z")
    reader = ScreeningRunReader(database)
    assert reader.latest_run().run_revision_id == "run-z"
    assert [r.run_revision_id for r in reader.list_runs()] == ["run-z", "run-a"]
    assert previous_run_revision_id(database, date(2026, 7, 9)) == "run-z"
    assert screening_run_payload(database, as_of_date=date(2026, 7, 8))["run_at"] == new["run_at"]
    # MCP uses ascending keyset pagination; compare and cursor use the same instant.
    first = stored_screening_rows(database, kind="screening_run", filters={}, after=None, limit=1)
    assert first[0]["run_revision_id"] == "run-a"
    after = [first[0]["asof_date"], first[0]["page_time"], first[0]["run_revision_id"]]
    second = stored_screening_rows(database, kind="screening_run", filters={}, after=after, limit=1)
    assert second[0]["run_revision_id"] == "run-z"
    store.prune(keep=1)
    assert [r.run_revision_id for r in reader.list_runs()] == ["run-z"]
