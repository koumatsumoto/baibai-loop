"""Master generation rebuilds sparse history from native L1, never previous L2."""

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pyarrow.parquet as pq
import pytest
from tests.helpers.lake_release import market_store
from tests.helpers.master import insert_master_rows
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.master_native import (
    HEAD_KEY,
    MasterAttempt,
    MasterL1,
)
from baibai_engine.machine_analysis.generation.master_inputs import read_master_input
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.stocks.reader import (
    read_master_snapshot,
)
from baibai_engine.machine_analysis.observations.stocks.storage_contracts import (
    JQUANTS_MASTER_SNAPSHOTS_DDL,
)
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_jobs.integration.master_native import (
    fixed_input,
    materialize_saved_input,
    model_input,
    rebuild,
    rebuild_saved_input,
)

DAY = date(2026, 10, 1)
NEXT = date(2026, 11, 1)
AT = datetime(2026, 11, 3, tzinfo=UTC)


def records(day=DAY):
    rows = [
        {
            "Date": day.isoformat(),
            "Code": f"{code}0",
            "CoName": f"合成企業{code}",
            "MktNm": "プライム" if code == 1301 else "OTHER",
            "S33Nm": "卸売業",
            "ProdCat": "011",
        }
        for code in range(1000, 3500)
    ]
    for code, category in (("80000", "ETF"), ("80010", "REIT"), ("13011", "011")):
        rows.append({**rows[0], "Code": code, "ProdCat": category, "UnknownNative": {"x": None}})
    return rows


def attempt(identity, *, day=DAY, at=AT, rows=None, partial=False):
    payload = {"data": records(day) if rows is None else rows}
    if partial:
        payload["pagination_key"] = "next"
    return MasterAttempt(
        identity,
        day,
        at,
        (
            JQuantsPage(
                "master", {"date": day.strftime("%Y%m%d")}, at, json.dumps(payload).encode()
            ),
        ),
    )


def setup_native(tmp_path, attempts):
    store = MemoryR2Store()
    owner = MasterL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit(attempts)
    market = market_store(tmp_path / "market.sqlite", short_sale_reports=())
    with sqlite3.connect(market) as connection:
        connection.executescript(JQUANTS_MASTER_SNAPSHOTS_DDL)
        insert_master_rows(
            connection, [(DAY.isoformat(), "9999", "stale999", "プライム", "情報・通信業", "011")]
        )
    return store, owner, market


def stored_rows(market):
    with sqlite3.connect(market) as connection:
        return connection.execute("SELECT * FROM jquants_master_snapshots ORDER BY 1,2").fetchall()


def test_sparse_history_all_products_and_unknown_fields_survive_cold_rebuild(tmp_path):
    store, owner, market = setup_native(tmp_path, [attempt("first"), attempt("next", day=NEXT)])
    store.get_keys.clear()
    fixed = owner.fixed()
    saved = model_input(
        store, fixed, as_of=NEXT + timedelta(days=1), evaluated_at=AT, market=market
    )
    pack_reads = [key for key in store.get_keys if "/master/v1/packs/" in key]
    assert len(pack_reads) == len(set(pack_reads)) == 1
    assert len(stored_rows(market)) == 5006
    assert not any(code == "99990" for _, code, _ in stored_rows(market))
    with sqlite3.connect(market) as connection:
        assert connection.execute(
            "SELECT coverage_start,record_count,status FROM source_coverage "
            "WHERE source='jquants_master_snapshots' ORDER BY 1"
        ).fetchall() == [(DAY.isoformat(), 2503, "ok"), (NEXT.isoformat(), 2503, "ok")]
    assert read_master_snapshot(market, as_of=DAY).as_of == DAY
    assert read_master_snapshot(market, as_of=NEXT + timedelta(days=1)).as_of == NEXT
    assert read_master_snapshot(market, as_of=DAY - timedelta(days=1)) is None
    selected = read_master_input(store, saved.descriptor, days=(DAY, DAY + timedelta(days=1), NEXT))
    assert set(selected["scopes"]) == {DAY.isoformat(), NEXT.isoformat()}
    assert saved.descriptor["summary"]["snapshot_days"] == 2
    assert saved.descriptor["summary"]["snapshot_status"] == "prior_snapshot"
    assert "scopes" not in saved.descriptor
    original = stored_rows(market)
    del store.values[HEAD_KEY]
    market.unlink()
    store.get_keys.clear()
    result = rebuild_saved_input(store, saved, start=DAY, end=NEXT, output=tmp_path / "cold")
    assert HEAD_KEY not in store.get_keys
    assert result["snapshot_days"] == 2
    assert result["unavailable_days"] == 0
    assert result["rows"] == 5006
    cold = pq.read_table(tmp_path / "cold/master_snapshots.parquet").to_pylist()
    assert [tuple(row.values()) for row in cold] == original
    secondary = next(
        json.loads(row["record_json"]) for row in cold if row["security_code"] == "13011"
    )
    assert secondary["UnknownNative"] == {"x": None}
    assert len(pq.read_table(tmp_path / "cold/master_receipt_status.parquet")) == 2
    assert not (tmp_path / "cold/head.json").exists()
    with pytest.raises(ValueError, match="outside"):
        rebuild_saved_input(
            store, saved, start=DAY - timedelta(days=1), end=NEXT, output=tmp_path / "outside"
        )


def test_corrections_withdraw_codes_and_change_classification_without_old_l2(tmp_path):
    store, owner, market = setup_native(tmp_path, [attempt("first"), attempt("next", day=NEXT)])
    corrected = [row for row in records(NEXT) if row["Code"] != "80010"]
    for row in corrected:
        if row["Code"] == "13010":
            row["ProdCat"] = "ETF"
            row["S33Nm"] = "情報・通信業"
    corrected.append({**corrected[0], "Code": "90000"})  # Still 2500 common primary lines.
    owner.commit([attempt("corrected", day=NEXT, at=AT + timedelta(hours=1), rows=corrected)])
    model_input(
        store, owner.fixed(), as_of=NEXT, evaluated_at=AT + timedelta(hours=1), market=market
    )
    prior, current = (
        read_master_snapshot(market, as_of=DAY),
        read_master_snapshot(market, as_of=NEXT),
    )
    assert prior.securities["1301"].is_common_stock
    assert not current.securities["1301"].is_common_stock
    assert current.securities["1301"].sector_33 == "情報・通信業"
    assert "8001" in prior.securities
    assert "8001" not in current.securities
    assert len(stored_rows(market)) == 5006


def test_replay_changes_only_different_fixed_rows_and_removes_only_selected_dates(tmp_path):
    store, owner, market = setup_native(tmp_path, [attempt("first"), attempt("next", day=NEXT)])
    saved = model_input(store, owner.fixed(), as_of=NEXT, evaluated_at=AT, market=market)
    expected = stored_rows(market)
    with sqlite3.connect(market) as connection:
        # Include malformed old TEXT and an unselected day: neither is native evidence.
        connection.execute(
            "UPDATE jquants_master_snapshots SET record_json=CAST(x'ff' AS TEXT) "
            "WHERE snapshot_date=? AND security_code='13011'",
            (DAY.isoformat(),),
        )
        connection.execute(
            "DELETE FROM jquants_master_snapshots WHERE snapshot_date=? AND security_code='80010'",
            (DAY.isoformat(),),
        )
        connection.executemany(
            "INSERT INTO jquants_master_snapshots VALUES (?, '99999', 'obsolete')",
            [(DAY.isoformat(),), ("1900-01-01",)],
        )
        connection.executescript(
            "CREATE TABLE writes(op,code); "
            "CREATE TRIGGER note_insert AFTER INSERT ON jquants_master_snapshots "
            "BEGIN INSERT INTO writes VALUES('insert',new.security_code); END;"
            "CREATE TRIGGER note_delete AFTER DELETE ON jquants_master_snapshots "
            "BEGIN INSERT INTO writes VALUES('delete',old.security_code); END;"
            "CREATE TRIGGER note_update AFTER UPDATE ON jquants_master_snapshots "
            "BEGIN INSERT INTO writes VALUES('update',new.security_code); END;"
        )
    materialize_saved_input(store, saved, market=market, days=[DAY])
    with sqlite3.connect(market) as connection:
        assert connection.execute("SELECT * FROM writes ORDER BY op").fetchall() == [
            ("delete", "99999"),
            ("insert", "80010"),
            ("update", "13011"),
        ]
        connection.execute("DELETE FROM writes")
    assert stored_rows(market) == [("1900-01-01", "99999", "obsolete"), *expected]
    assert model_input(store, owner.fixed(), as_of=NEXT, evaluated_at=AT, market=market) == saved
    with sqlite3.connect(market) as connection:
        assert connection.execute("SELECT * FROM writes").fetchall() == [("delete", "99999")]
    assert stored_rows(market) == expected


@pytest.mark.parametrize("failure", ["row", "coverage"])
def test_failed_master_correction_leaves_previous_rows_and_coverage_intact(tmp_path, failure):
    store, owner, market = setup_native(tmp_path, [attempt("previous"), attempt("next", day=NEXT)])
    model_input(store, owner.fixed(), as_of=NEXT, evaluated_at=AT, market=market)
    original = stored_rows(market)
    revised = [row for row in records() if row["Code"] != "80010"]
    revised[0]["CoName"] = "corrected"
    revised.append({**revised[0], "Code": "99000"})
    later = AT + timedelta(hours=1)
    owner.commit([attempt("revised", at=later, rows=revised)])
    saved = fixed_input(store, owner.fixed(), days=[DAY], as_of=DAY, evaluated_at=later)

    coverage_sql = "SELECT * FROM source_coverage ORDER BY source, coverage_key"
    trigger = (
        "BEFORE INSERT ON jquants_master_snapshots WHEN NEW.security_code='99000'"
        if failure == "row"
        else "BEFORE INSERT ON source_coverage WHEN NEW.source='jquants_master_snapshots'"
    )
    with sqlite3.connect(market) as conn:
        preceding = conn.execute(coverage_sql).fetchall()
        conn.execute(
            f"CREATE TRIGGER interrupt {trigger} BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        materialize_saved_input(store, saved, market=market, days=[DAY])
    assert stored_rows(market) == original
    with sqlite3.connect(market) as conn:
        assert conn.execute(coverage_sql).fetchall() == preceding
        conn.execute("DROP TRIGGER interrupt")

    materialize_saved_input(store, saved, market=market, days=[DAY])
    corrected = read_master_snapshot(market, as_of=DAY)
    assert corrected is not None
    assert corrected.securities["1000"].name == "corrected"
    assert "9900" in corrected.securities
    assert "8001" not in corrected.securities
    assert [row for row in stored_rows(market) if row[0] == NEXT.isoformat()] == [
        row for row in original if row[0] == NEXT.isoformat()
    ]


def test_invalid_later_required_snapshot_rolls_back_an_earlier_valid_change(tmp_path):
    store, owner, market = setup_native(tmp_path, [attempt("first"), attempt("next", day=NEXT)])
    model_input(store, owner.fixed(), as_of=NEXT, evaluated_at=AT, market=market)
    original = stored_rows(market)
    with sqlite3.connect(market) as connection:
        coverage = connection.execute("SELECT * FROM source_coverage ORDER BY 1,2").fetchall()
    revised = records(DAY)
    revised[0]["CoName"] = "更新された会社名"
    later = AT + timedelta(hours=1)
    owner.commit(
        [
            attempt("changed", rows=revised, at=later),
            attempt("invalid", day=NEXT, rows=[], at=later),
        ]
    )
    with pytest.raises(JQuantsProviderError, match="empty"):
        model_input(store, owner.fixed(), as_of=NEXT, evaluated_at=later, market=market)
    assert stored_rows(market) == original
    with sqlite3.connect(market) as connection:
        assert (
            connection.execute("SELECT * FROM source_coverage ORDER BY 1,2").fetchall() == coverage
        )


@pytest.mark.parametrize(
    "invalid", ["empty", "truncated", "wrong-date", "duplicate", "missing-field"]
)
def test_invalid_adopted_master_never_replaces_previous_execution_rows(tmp_path, invalid):
    store, owner, market = setup_native(tmp_path, [attempt("first")])
    rows = records()
    if invalid == "empty":
        rows = []
    elif invalid == "truncated":
        rows = rows[:2499]
    elif invalid == "wrong-date":
        rows[-1]["Date"] = NEXT.isoformat()
    elif invalid == "duplicate":
        rows.append(dict(rows[0]))
    else:
        del rows[-1]["ProdCat"]
    owner.commit([attempt("invalid", at=AT + timedelta(hours=1), rows=rows)])
    old = stored_rows(market)
    with pytest.raises(JQuantsProviderError):
        model_input(
            store, owner.fixed(), as_of=DAY, evaluated_at=AT + timedelta(hours=1), market=market
        )
    assert stored_rows(market) == old
    assert owner.fixed().value()["scopes"][DAY.isoformat()]["adopted"] == "invalid"


@pytest.mark.parametrize("held", [False, True])
def test_newer_failed_or_held_keeps_native_adopted_with_explicit_partial(tmp_path, held):
    store, owner, market = setup_native(tmp_path, [attempt("first")])
    changed = records()
    changed[0]["CoName"] = "changed"
    owner.commit(
        [
            attempt(
                "unresolved",
                at=AT if held else AT + timedelta(hours=1),
                rows=changed,
                partial=not held,
            )
        ]
    )
    saved = model_input(
        store, owner.fixed(), as_of=DAY, evaluated_at=AT + timedelta(hours=1), market=market
    )
    assert json.loads(stored_rows(market)[0][2])["CoName"] != "changed"
    assert read_master_snapshot(market, as_of=DAY).as_of == DAY
    assert saved.descriptor["summary"]["coverage_status"] == "partial"
    assert saved.descriptor["summary"]["held_days" if held else "failed_days"] == 1
    result = rebuild_saved_input(store, saved, start=DAY, end=DAY, output=tmp_path / "cold")
    assert result["rows"] == 2503
    assert result["partial_days"] == 1


def test_future_confirmation_rejected_before_any_projection_change(tmp_path):
    store, owner, market = setup_native(tmp_path, [attempt("first")])
    owner.commit([attempt("confirmed", at=AT + timedelta(hours=1))])
    previous = stored_rows(market)
    with pytest.raises(ValueError, match="confirmation state is later"):
        model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    assert stored_rows(market) == previous


def test_preflight_materializes_only_explicit_exact_date_without_head(tmp_path):
    store, owner, market = setup_native(tmp_path, [attempt("first"), attempt("next", day=NEXT)])
    saved = fixed_input(store, owner.fixed(), days=(NEXT,), as_of=NEXT, evaluated_at=AT)
    del store.values[HEAD_KEY]
    store.get_keys.clear()
    materialize_saved_input(store, saved, market=market, days=(NEXT,))
    assert HEAD_KEY not in store.get_keys
    assert read_master_snapshot(market, as_of=NEXT).as_of == NEXT
    assert any(code == "99990" for _, code, _ in stored_rows(market))
    with pytest.raises(ValueError, match="outside"):
        materialize_saved_input(store, saved, market=market, days=(DAY,))


def test_cold_sparse_gap_has_no_manufactured_daily_status(tmp_path):
    store, owner, _market = setup_native(tmp_path, [attempt("first"), attempt("next", day=NEXT)])
    result = rebuild(
        store,
        owner.fixed(),
        start=DAY + timedelta(days=1),
        end=NEXT - timedelta(days=1),
        output=tmp_path / "gap",
    )
    assert result["snapshot_days"] == result["rows"] == result["unavailable_days"] == 0
    assert len(pq.read_table(tmp_path / "gap/master_receipt_status.parquet")) == 0


def test_unavailable_requested_snapshot_does_not_reuse_old_projection(tmp_path):
    store, owner, market = setup_native(tmp_path, [attempt("failed", partial=True)])
    saved = model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    assert stored_rows(market) == []
    assert saved.descriptor["summary"]["unavailable_days"] == 1
    assert saved.descriptor["summary"]["snapshot_status"] == "unavailable"
    with sqlite3.connect(market) as connection:
        assert connection.execute(
            "SELECT status,record_count FROM source_coverage WHERE source='jquants_master_snapshots'"
        ).fetchall() == [("unavailable", 0)]
    assert read_master_snapshot(market, as_of=DAY) is None


def test_saved_metadata_missing_fails_without_latest_fallback(tmp_path):
    from baibai_engine.storage.objects.r2 import LakePublishError

    store, owner, market = setup_native(tmp_path, [attempt("first")])
    saved = model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    root = (
        "fixed-inputs/jquants-master/v1/selections/"
        + saved.descriptor["selection_sha256"]
        + ".json"
    )
    del store.values[root]
    store.get_keys.clear()
    with pytest.raises(LakePublishError, match="unavailable"):
        rebuild_saved_input(store, saved, start=DAY, end=DAY, output=tmp_path / "missing")
    assert HEAD_KEY not in store.get_keys


def test_future_business_snapshot_is_retained_but_never_generation_asof(tmp_path):
    store, owner, market = setup_native(tmp_path, [attempt("past"), attempt("future", day=NEXT)])
    saved = model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    assert saved.descriptor["summary"]["snapshot_date"] == DAY.isoformat()
    assert read_master_snapshot(market, as_of=DAY).as_of == DAY
    assert len(stored_rows(market)) == 5006


def test_base_export_still_rejects_empty_without_native_composition(tmp_path):
    from baibai_engine.storage.snapshots.writer import LakeBuildError, export_market_snapshot

    market = market_store(tmp_path / "market.sqlite")
    with (
        sealed_sqlite_snapshot(sqlite_path=market, mirror_root=tmp_path / "seal") as snapshot,
        pytest.raises(LakeBuildError, match="no lake dataset holds a row"),
    ):
        export_market_snapshot(
            catalog={},
            source_snapshot=snapshot,
            mirror_root=tmp_path / "export",
            producer_git_commit="a" * 40,
            expected_store_origin=None,
        )


@pytest.mark.parametrize("invalid_kind", ["empty", "truncated", "wrong-date"])
def test_historical_invalid_is_unavailable_boundary_but_newer_valid_recovers(
    tmp_path, invalid_kind
):
    prior = DAY - timedelta(days=30)
    invalid = (
        []
        if invalid_kind == "empty"
        else records()[:2499]
        if invalid_kind == "truncated"
        else records(NEXT)
    )
    store, owner, market = setup_native(
        tmp_path,
        [
            attempt("prior", day=prior),
            attempt("invalid", rows=invalid),
            attempt("current", day=NEXT),
        ],
    )
    saved = model_input(store, owner.fixed(), as_of=NEXT, evaluated_at=AT, market=market)
    summary = saved.descriptor["summary"]
    assert summary["coverage_status"] == "partial"
    assert summary["invalid_days"] == summary["unavailable_days"] == 1
    assert summary["invalid_snapshot_dates"] == [DAY.isoformat()]
    assert summary["snapshot_date"] == NEXT.isoformat()
    assert read_master_snapshot(market, as_of=DAY - timedelta(days=1)).as_of == prior
    assert read_master_snapshot(market, as_of=prior - timedelta(days=1)) is None
    assert read_master_snapshot(market, as_of=DAY, tickers=("1234",)) is None
    for day in (DAY, DAY + timedelta(days=1), NEXT - timedelta(days=1)):
        assert read_master_snapshot(market, as_of=day) is None
    assert read_master_snapshot(market, as_of=NEXT).as_of == NEXT
    old = stored_rows(market)
    with pytest.raises(JQuantsProviderError):
        model_input(store, owner.fixed(), as_of=DAY, evaluated_at=AT, market=market)
    assert stored_rows(market) == old
    del store.values[HEAD_KEY]
    market.unlink()
    store.get_keys.clear()
    cold = rebuild_saved_input(store, saved, start=prior, end=NEXT, output=tmp_path / "cold")
    assert cold["invalid_days"] == 1
    assert cold["coverage_status"] == "partial"
    statuses = pq.read_table(tmp_path / "cold/master_receipt_status.parquet").to_pylist()
    invalid_status = next(row for row in statuses if row["day"] == DAY.isoformat())
    assert invalid_status["status"] == "unavailable"
    assert invalid_status["error"].startswith("invalid master snapshot: ")
    assert invalid_status["row_count"] == 0
    assert HEAD_KEY not in store.get_keys
    # A historical slice reproduces its known invalid state, not a fabricated prior universe.
    subset = rebuild_saved_input(store, saved, start=DAY, end=DAY, output=tmp_path / "subset")
    assert subset["rows"] == 0
    assert subset["invalid_days"] == 1
    assert subset["coverage_status"] == "partial"
