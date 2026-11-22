"""月/v2を維持し、日足の同月追加で過去日を毎日複製しない。"""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.master_calendar_native import (
    native_master_calendar_publication as native_master_calendar_publication,
)
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.price_native import (
    HEAD_KEY,
    PriceAttempt,
    PriceL1,
)
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.price_inputs import (
    PRICE_PROJECTION_REVISION,
    read_price_input,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.hydrate import hydrate_market_store
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import (
    iter_partition_rows,
    materialize_partitions,
    resolve_current_release,
)
from baibai_engine.storage.snapshots.writer import LakeBuildError, validate_snapshot_parity
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    read_lake_store_origin,
    write_lake_store_origin,
)
from baibai_jobs.integration.price_native import model_input, rebuild_saved_input
from baibai_jobs.publication import l1_price_export, snapshot_write_cli
from baibai_jobs.publication import snapshot_publication as publication
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile

DAY = date(2026, 10, 1)
AT = datetime(2026, 11, 1, tzinfo=UTC)
DATASET = LAKE_DATASETS["jquants.daily_bars"]


@pytest.mark.parametrize("foreign_format", ["jquants-financial-input-v2", None])
def test_price_export_rejects_foreign_format_even_with_a_valid_digest(
    tmp_path, mocker, foreign_format
):
    descriptor = {
        "format": foreign_format,
        "selection_sha256": "f" * 64,
        "summary": {
            "selection_start": DAY.isoformat(),
            "selection_end": DAY.isoformat(),
            "coverage_status": "complete",
            "projection_revision": PRICE_PROJECTION_REVISION,
        },
    }
    saved = InputSnapshot(
        kind="native_l1",
        reference="foreign-fixed-input",
        sha256=hashlib.sha256(canonical_json(descriptor).encode()).hexdigest(),
        descriptor=descriptor,
    )
    store = MemoryR2Store()
    export = mocker.patch.object(l1_price_export, "export_dataset_snapshot")
    with pytest.raises(ValueError, match="unsupported fixed price input format"):
        read_price_input(store, descriptor, days=(DAY,))
    with pytest.raises(ValueError, match="unsupported fixed price input format"):
        l1_price_export.export_prices_from_native_input(
            source_snapshot=SimpleNamespace(path=tmp_path / "unused.sqlite"),
            saved=saved,
            mirror_root=tmp_path / "untouched",
            producer_git_commit="a" * 40,
        )
    assert store.get_keys == store.put_keys == []
    assert not (tmp_path / "untouched").exists()
    export.assert_not_called()


def records(day, *, revision=0, size=16):
    return [
        {
            "Code": f"{1000 + index}0",
            "Date": day.isoformat(),
            "O": 100 + index,
            "H": 120 + index,
            "L": 90 + index,
            "C": None if index == 0 else 110 + index + revision,
            "Vo": None if index == 0 else index * 13,
            "Va": 0 if index == 1 else None,
            "AdjFactor": 0.5 if index == 2 else 1,
            "UL": "0" if index == 0 else None,
            "UnknownNative": {"nullable": None},
        }
        for index in range(size)
    ]


def attempt(identity, day, *, tick=0, rows=None, size=16):
    instant = AT + timedelta(minutes=tick)
    return PriceAttempt(
        identity,
        day,
        instant,
        (
            JQuantsPage(
                "daily_bars",
                {"date": day.isoformat()},
                instant,
                json.dumps({"data": records(day, size=size) if rows is None else rows}).encode(),
            ),
        ),
    )


def configure(monkeypatch):
    narrow_release_policy(monkeypatch, datasets=(DATASET.name,))
    monkeypatch.setattr(publication, "LAKE_DATASETS", {DATASET.name: DATASET})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)


def publish(store, market, mirror, *, identity, asof):
    store.put_keys.clear()
    with publication.prepare_market_lake(
        sqlite_path=market,
        mirror_root=mirror,
        store=store,
        as_of=asof,
        release_id=identity,
    ) as prepared:
        manifest = prepared.export.datasets[DATASET.name].manifest
        saved = prepared.price_identity
        publication.publish_prepared_market_lake(prepared, store=store)
    keys = [
        key for key in store.put_keys if f"/{DATASET.name}/" in key and key.endswith(".parquet")
    ]
    return manifest, saved, keys


def read_rows(mirror, manifest, cache_root):
    paths = materialize_partitions(
        LakeObjectCache(root=cache_root, source=LocalMirrorSource(mirror)),
        dataset=DATASET,
        partitions=manifest.partitions,
    )
    with lake_session() as session:
        return [
            row
            for batch in iter_partition_rows(session, dataset=DATASET, paths=paths)
            for row in batch
        ]


def test_actual_publication_keeps_old_month_and_reuses_daily_objects(tmp_path, monkeypatch):
    configure(monkeypatch)
    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    previous = DAY - timedelta(days=1)
    second, added = DAY + timedelta(days=1), DAY + timedelta(days=2)
    owner.commit([attempt("previous", previous), attempt("first", DAY), attempt("second", second)])
    market = market_store(tmp_path / "market.sqlite", daily_bars=(), short_sale_reports=())
    mirror = tmp_path / "mirror"
    exporter = l1_price_export.export_dataset_snapshot

    def old_month_export(**kwargs):
        kwargs.pop("object_date_grain")
        return exporter(**kwargs)

    with monkeypatch.context() as old:
        old.setattr(l1_price_export, "export_dataset_snapshot", old_month_export)
        original, saved, keys = publish(store, market, mirror, identity="old-month", asof=second)
    assert len(keys) == 2
    original_bytes = {key: store.values[key].body for key in keys}
    original_rows = read_rows(mirror, original, tmp_path / "old-reader")
    assert len(original_rows) == 48
    assert original_rows[0][5:8] == (None, None, None)
    assert next(row for row in original_rows if row[0] == "1001")[7] == 0

    changes, manifests = [], []
    for index, received in enumerate(
        (
            None,
            attempt("equal", DAY, tick=1),
            attempt("added", added, tick=2),
            attempt("corrected", DAY, tick=3, rows=records(DAY, revision=20)),
            attempt("withdrawn", second, tick=4, rows=[]),
        )
    ):
        if received is not None:
            owner.commit([received])
        manifest, _, keys = publish(
            store, market, mirror, identity=f"daily-{index}", asof=second if index < 2 else added
        )
        changes.append(len(keys))
        manifests.append(manifest)
        assert manifest.contract_version == original.contract_version == 2
        assert manifest.partition_by == original.partition_by == ("year", "month")
        assert all(
            obj.rows == 16 and obj.min_key[1] == obj.max_key[1]
            for part in manifest.partitions
            for obj in part.objects
        )
    assert changes == [2, 0, 1, 1, 0]
    assert {key: store.values[key].body for key in original_bytes} == original_bytes
    assert read_rows(mirror, original, tmp_path / "still-old") == original_rows
    final = manifests[-1]
    with sqlite3.connect(market) as connection:
        expected = connection.execute(
            "SELECT * FROM jquants_daily_bars ORDER BY ticker,traded_at"
        ).fetchall()
    assert read_rows(mirror, final, tmp_path / "new-reader") == expected
    assert len(expected) == 48
    assert not any(row[1] == second.isoformat() for row in expected)
    assert next(row for row in expected if row[:2] == ("1001", DAY.isoformat()))[5] == 131
    shuffled = final.model_copy(
        update={
            "partitions": tuple(
                part.model_copy(update={"objects": tuple(reversed(part.objects))})
                for part in final.partitions
            )
        }
    )
    validate_snapshot_parity(
        dataset=DATASET, sqlite_path=market, mirror_root=mirror, manifest=shuffled
    )
    missing = final.model_copy(
        update={
            "partitions": tuple(
                part.model_copy(update={"objects": part.objects[:1]}) for part in final.partitions
            )
        }
    )
    with pytest.raises(LakeBuildError, match=r"omit SQLite|differ from SQLite"):
        validate_snapshot_parity(
            dataset=DATASET, sqlite_path=market, mirror_root=mirror, manifest=missing
        )

    hydrated = market_store(tmp_path / "hydrated.sqlite", daily_bars=(), short_sale_reports=())
    with lake_session() as session:
        hydrate_market_store(
            session,
            catalog={DATASET.name: DATASET},
            release=resolve_current_release(
                SimpleNamespace(read_bytes=store.get_bytes),
                catalog={DATASET.name: DATASET},
                policy=release_policy_for_profile("production"),
            ),
            cache=LakeObjectCache(
                root=tmp_path / "hydrate-cache", source=LocalMirrorSource(mirror)
            ),
            store=hydrated,
            dataset_names=(DATASET.name,),
        )
    with sqlite3.connect(hydrated) as connection:
        assert (
            connection.execute(
                "SELECT * FROM jquants_daily_bars ORDER BY ticker,traded_at"
            ).fetchall()
            == expected
        )

    # A past generation's native selection survives both a newer correction and L2 removal.
    del store.values[HEAD_KEY]
    for key in list(store.values):
        if key.startswith("lake/"):
            del store.values[key]
    market.unlink()
    store.get_keys.clear()
    counts = rebuild_saved_input(store, saved, start=previous, end=second, output=tmp_path / "cold")
    assert counts["rows"] == 48
    assert HEAD_KEY not in store.get_keys
    assert not any(key.startswith("lake/") for key in store.get_keys)
    cold = pq.read_table(tmp_path / "cold/daily_bars.parquet").to_pylist()
    columns = [column.name for column in DATASET.columns]
    assert sorted(tuple(row[name] for name in columns) for row in cold) == original_rows


def test_daily_publisher_reduces_retained_rows_and_bytes_for_same_month(tmp_path, monkeypatch):
    configure(monkeypatch)
    exporter = l1_price_export.export_dataset_snapshot
    totals = {}
    for layout in ("month", "day"):
        root = tmp_path / layout
        root.mkdir()
        store = MemoryR2Store()
        owner = PriceL1(store, root / "native")
        owner.initialize()
        market = market_store(root / "market.sqlite", daily_bars=(), short_sale_reports=())
        object_rows, uploaded_bytes = [], 0
        with monkeypatch.context() as mode:
            if layout == "month":

                def monthly(**kwargs):
                    kwargs.pop("object_date_grain")
                    return exporter(**kwargs)

                mode.setattr(l1_price_export, "export_dataset_snapshot", monthly)
            for index in range(6):
                day = DAY + timedelta(days=index)
                rows = records(day, size=128)
                for number, row in enumerate(rows, 1):
                    for field_index, field in enumerate(("O", "H", "L", "C", "Vo", "Va"), 1):
                        if row[field] is not None:
                            row[field] += (number * (day.day + field_index)) ** 2 * 0.0107
                owner.commit([attempt(str(index), day, rows=rows)])
                _, _, keys = publish(
                    store, market, root / "mirror", identity=f"{layout}-{index}", asof=day
                )
                assert len(keys) == 1
                object_rows.append(pq.read_metadata(root / "mirror" / keys[0]).num_rows)
                uploaded_bytes += sum(len(store.values[key].body) for key in keys)
        totals[layout] = {"rows": object_rows, "bytes": uploaded_bytes}
    assert totals["month"]["rows"] == [128 * day for day in range(1, 7)]
    assert totals["day"]["rows"] == [128] * 6
    assert totals["day"]["bytes"] < totals["month"]["bytes"]
    print("native price actual publisher:", totals)


def test_publication_uses_native_price_input_without_rollout_mode(tmp_path, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "false")
    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit(
        [
            attempt(
                "native",
                DAY,
                rows=[
                    {
                        "Code": "13010",
                        "Date": DAY.isoformat(),
                        "C": 100,
                        "AdjFactor": 0.5,
                        "ExRT": "1",
                    }
                ],
            )
        ]
    )
    market = market_store(
        tmp_path / "market.sqlite",
        daily_bars=(("1301", DAY.isoformat(), 999.0, 12.0),),
        short_sale_reports=(),
    )
    with publication.prepare_market_lake(
        sqlite_path=market, mirror_root=tmp_path / "mirror", store=store, as_of=DAY
    ) as prepared:
        assert prepared.price_identity.kind == "native_l1"
        manifest = prepared.export.datasets[DATASET.name].manifest
        assert manifest.contract_version == 2
        assert all(ref.kind == "native_l1" for part in manifest.partitions for ref in part.sources)
        with sqlite3.connect(prepared.sqlite_path) as connection:
            assert connection.execute(
                "SELECT close, adjustment_factor, volume, ex_right_type FROM jquants_daily_bars"
            ).fetchall() == [(100.0, 0.5, None, "1")]


def test_daily_hydrate_rebuilds_native_prices_but_explicit_hydrate_still_needs_old_objects(
    tmp_path, monkeypatch
):
    names = (DATASET.name, "jquants.market_calendar", "jquants.master_snapshots")
    narrow_release_policy(monkeypatch, datasets=names)
    monkeypatch.setattr(publication, "LAKE_DATASETS", {name: LAKE_DATASETS[name] for name in names})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    second = DAY + timedelta(days=1)
    owner.commit([attempt("first", DAY), attempt("second", second)])
    src = market_store(tmp_path / "source.sqlite", daily_bars=(), short_sale_reports=())
    mirror = tmp_path / "mirror"
    previous, _, _ = publish(store, src, mirror, identity="previous", asof=second)
    origin = read_lake_store_origin(src)
    assert origin is not None
    transfer = market_store(tmp_path / "transfer.sqlite", daily_bars=(), short_sale_reports=())
    with closing(sqlite3.connect(transfer)) as conn, conn:
        write_lake_store_origin(conn, origin)

    def fill(command):
        return snapshot_write_cli.main(
            [
                command,
                "--mirror",
                str(mirror),
                "--store",
                str(transfer),
                "--release",
                origin.release_id,
                "--manifest-sha256",
                origin.release_manifest_sha256,
            ]
        )

    # No previous price body can be read; only a normal daily transfer can defer it.
    (mirror / previous.partitions[0].objects[0].key).unlink()
    assert fill("hydrate") == 1
    assert fill("hydrate-portfolio") == 1
    assert fill("hydrate-for-daily") == 0
    with closing(sqlite3.connect(transfer)) as conn:
        assert conn.execute("SELECT count(*) FROM jquants_daily_bars").fetchone() == (0,)
        for table in ("jquants_market_calendar", "jquants_master_snapshots"):
            assert conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() == (1,)

    # A gap in the old Lake never substitutes a stale quote for a native correction or withdrawal.
    owner.commit(
        [
            attempt("correction", DAY, tick=1, rows=records(DAY, revision=9)),
            attempt("withdrawn", second, tick=1, rows=[]),
        ]
    )
    saved = model_input(
        store, owner.fixed(), as_of=second, evaluated_at=AT + timedelta(days=1), market=transfer
    )
    assert saved.descriptor["summary"]["normal_empty_days"] == 1
    with closing(sqlite3.connect(transfer)) as conn, conn:
        assert conn.execute(
            "SELECT close FROM jquants_daily_bars WHERE ticker='1001'"
        ).fetchall() == [(120.0,)]
        assert (
            conn.execute(
                "SELECT 1 FROM jquants_daily_bars WHERE traded_at=?", (second.isoformat(),)
            ).fetchone()
            is None
        )
        # A populated transfer is not the disposable empty daily case.
    assert fill("hydrate-for-daily") == 1
    with closing(sqlite3.connect(transfer)) as conn, conn:
        conn.execute("DELETE FROM jquants_daily_bars")
        write_lake_store_origin(conn, LakeStoreOrigin("other", "b" * 64))
    assert fill("hydrate-for-daily") == 1

    with closing(sqlite3.connect(transfer)) as conn, conn:
        write_lake_store_origin(conn, origin)
    initial = snapshot_write_cli._daily_rebuilds_prices

    def raced(release, path):
        assert initial(release, path)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "INSERT INTO jquants_daily_bars(ticker,traded_at,close) VALUES('1001',?,17)",
                (DAY.isoformat(),),
            )
        return True

    monkeypatch.setattr(snapshot_write_cli, "_daily_rebuilds_prices", raced)
    assert fill("hydrate-for-daily") == 1
    with closing(sqlite3.connect(transfer)) as conn:
        assert conn.execute("SELECT close FROM jquants_daily_bars").fetchall() == [(17.0,)]


pytestmark = pytest.mark.usefixtures("native_master_calendar_publication")
