from __future__ import annotations

import sqlite3
from datetime import date
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


def test_old_rules_rerun_has_a_new_method_hash_while_saved_publication_stays_original(tmp_path):
    import json
    from copy import deepcopy

    from baibai_engine.screening.discovery.review_set import validate_review_set_payload
    from baibai_engine.screening.rules_identity import production_rules_contract_hash

    # Fingerprint of the pre-#1483 calculation at main 92c9fcb3, using this frozen rules file.
    legacy_hash = "eb22ae9970e2d8b89801ac987e3d6bc20a2e034581e4333e8d589f5500d80e76"
    rules = load_screening_rules(Path("method/screening/rules/2026-09-23T103000+0900.yaml"))
    row = _analysis()
    row["per_forward"] = row["per_trailing"] = None
    row["metrics"].update(
        normalized_per_3fy=None,
        asset_backed_ratio=None,
        operating_profit_ttm=12.0,
        same_state_equity_yen=120.0,
        fcf_yield=-0.01,
    )
    rerun = build_review_set(
        [row], rules=rules.candidate_discovery, required_jpx_flags=rules.universe.required_jpx_flags
    )
    assert rerun["method"]["method_id"] == "multi-valuation-v4"
    assert rerun["method"]["method_hash"] != legacy_hash
    assert rerun["entries"][0]["nominations"][0]["valuation_approach_id"] == "reinvestment-value"

    # Seed the old immutable publication; the current writer must not synthesize its history.
    legacy = deepcopy(rerun)
    legacy["method"]["method_hash"] = legacy_hash
    legacy["entries"] = []  # The old positive-FCF gate did not nominate this row.
    legacy["diagnostics"].update(
        nomination_counts=dict.fromkeys(legacy["diagnostics"]["nomination_counts"], 0),
        unique_candidate_count=0,
        review_set_count=0,
    )
    rules_hash = production_rules_contract_hash(rules.model_dump_json())
    legacy.update(
        review_set_id="saved-v4",
        run_revision_id="run-v4",
        as_of="2026-10-12",
        created_at="2026-10-12T18:00:00+09:00",
        screening_rules_hash=rules_hash,
    )
    database = tmp_path / "runs.sqlite"
    run = {
        **_run(),
        "run_id": "screening-20261012",
        "run_date": "2026-10-12",
        "asof_date": "2026-10-12",
        "run_at": "2026-10-12T18:00:00+09:00",
        "screening_rules_hash": rules_hash,
        "security_analyses": [row],
    }
    ScreeningRunStore(database).publish_run(run, run_revision_id="run-v4")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO review_set VALUES (?,?,?,?)",
            ("saved-v4", "run-v4", legacy["created_at"], json.dumps(legacy)),
        )
    original = database.read_bytes()
    reader = ScreeningRunReader(database)
    saved = reader.get_review_set("saved-v4")
    assert saved.payload == legacy
    assert reader.get_run("run-v4").security_analyses == (row,)
    assert database.read_bytes() == original
    with pytest.raises(ReviewSetContractError, match="does not match"):
        validate_review_set_payload(
            legacy,
            security_analyses=[row],
            rules=rules.candidate_discovery,
            required_jpx_flags=rules.universe.required_jpx_flags,
        )


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


def _publish_pair(
    store: ScreeningRunStore, payload: dict[str, object], *, revision: str | None = None
):
    rules = load_screening_rules()
    return store.publish_run(
        payload,
        run_revision_id=revision,
        review_set_rules=rules.candidate_discovery,
        required_jpx_flags=rules.universe.required_jpx_flags,
    )


def test_paired_run_and_review_set_retry_reuses_both_identities(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(path)
    first = _publish_pair(store, _run(), revision="paired-run")
    assert first.inserted
    assert first.review_set_id is not None
    with sqlite3.connect(path) as connection:
        before_dump = tuple(connection.iterdump())
    retry = _publish_pair(store, _run())
    assert not retry.inserted
    assert retry.publication_id == first.publication_id
    assert retry.review_set_id == first.review_set_id
    with sqlite3.connect(path) as connection:
        assert tuple(connection.iterdump()) == before_dump
    pair = ScreeningRunReader(path).get_review_set(first.review_set_id)
    assert pair is not None
    assert pair.payload["run_revision_id"] == first.publication_id
    assert pair.payload["created_at"] == _run()["run_at"]


@pytest.mark.parametrize("failed_table", ["security_analysis", "review_set"])
def test_paired_insert_failure_rolls_back_every_new_row_and_preserves_history(
    tmp_path: Path, failed_table: str
) -> None:
    path = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(path)
    old_pair = _publish_pair(store, _run(), revision="old-pair")
    unpaired = {**_run(), "run_at": "2026-07-08T18:10:00+09:00"}
    store.publish_run(unpaired, run_revision_id="retained-unpaired")
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"CREATE TRIGGER reject_new BEFORE INSERT ON {failed_table} "
            "BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END"
        )
        before = tuple(connection.iterdump())
    new_run = {**_run(), "run_at": "2026-07-08T19:00:00+09:00"}
    with pytest.raises(sqlite3.IntegrityError, match="injected insert failure"):
        _publish_pair(store, new_run, revision="new-pair")
    with sqlite3.connect(path) as connection:
        assert tuple(connection.iterdump()) == before
    reader = ScreeningRunReader(path)
    assert reader.get_run("new-pair") is None
    assert reader.get_run("retained-unpaired") is not None
    assert old_pair.review_set_id is not None
    assert reader.get_review_set(old_pair.review_set_id) is not None


def test_review_set_retry_helper_does_not_rollback_the_parent_transaction(tmp_path: Path) -> None:
    from baibai_engine.screening.discovery.contracts import PublishedReviewSet
    from baibai_engine.screening.run_store.store import connect_rw

    path = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(path)
    result = _publish_pair(store, _run(), revision="paired-run")
    assert result.review_set_id is not None
    stored = ScreeningRunReader(path).get_review_set(result.review_set_id)
    assert stored is not None
    publication = PublishedReviewSet.model_validate(stored.payload)
    with connect_rw(path) as connection:
        connection.execute("CREATE TABLE parent_work (value TEXT)")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO parent_work VALUES ('keep this commit')")
        retry = store._publish_review_set_in_transaction(
            connection,
            publication,
            source_payload_json=str(
                connection.execute(
                    "SELECT payload FROM screening_run WHERE run_revision_id = ?",
                    (publication.run_revision_id,),
                ).fetchone()[0]
            ),
        )
        assert not retry.inserted
        assert connection.in_transaction
        connection.commit()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM parent_work").fetchall() == [
            ("keep this commit",)
        ]


def test_paired_empty_set_and_retained_unpaired_run_are_valid(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(path)
    store.publish_run(_run(), run_revision_id="retained-unpaired")
    empty_run = {**_run(), "run_at": "2026-07-08T19:00:00+09:00", "security_analyses": []}
    result = _publish_pair(store, empty_run)
    assert result.review_set_id is not None
    reader = ScreeningRunReader(path)
    empty_set = reader.get_review_set(result.review_set_id)
    assert empty_set is not None
    assert empty_set.payload["entries"] == []
    assert reader.get_run("retained-unpaired") is not None
    assert (
        reader.valuation_run(as_of=date(2026, 7, 8), run_revision_id="retained-unpaired")
        is not None
    )


def test_paired_conflicting_identity_preserves_existing_pair(tmp_path: Path) -> None:
    from baibai_engine.screening.run_store.store import RunStoreConflictError

    path = tmp_path / "runs.sqlite"
    store = ScreeningRunStore(path)
    _publish_pair(store, _run(), revision="paired-run")
    with sqlite3.connect(path) as connection:
        before = tuple(connection.iterdump())
    for payload, revision in [({**_run(), "universe_size": 2}, "paired-run"), (_run(), "wrong-id")]:
        with pytest.raises(RunStoreConflictError):
            _publish_pair(store, payload, revision=revision)
    with sqlite3.connect(path) as connection:
        assert tuple(connection.iterdump()) == before
