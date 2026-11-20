"""SQLite転送の順序を変えても全値・固定入力・coverageと原子性を保つ。"""

import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Barrier, Lock

import pyarrow.parquet as pq
import pytest
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.price_native import (
    MAX_PACK_BYTES,
    PREFIX,
    PriceAttempt,
    PriceL1,
)
from baibai_engine.machine_analysis.generation.price_inputs import read_price_input
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.machine_analysis.observations.prices.price_observation_reader import (
    read_price_day_states,
)
from baibai_engine.storage.objects.r2 import LakePublishError, R2Error
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_jobs.integration import price_native
from baibai_jobs.publication.l1_price_export import export_prices_from_native_input

DAY = date(2026, 10, 31)
AT = datetime(2026, 11, 4, tzinfo=UTC)
TABLES = ("jquants_daily_bars", "jquants_source_pages", "source_coverage")


def rows_at(path):
    with sqlite3.connect(path) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in TABLES
        }


@pytest.fixture
def fixed_prices(tmp_path):
    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    attempts = []
    for offset in range(3):
        day = DAY + timedelta(days=offset)
        rows = (
            []
            if offset == 1
            else [
                {
                    "Code": ticker,
                    "Date": day.isoformat(),
                    "O": 101 + offset,
                    "H": 105 + offset,
                    "L": 99 + offset,
                    "C": 103 + offset,
                    "Vo": 1234,
                    "Va": 127102,
                    "AdjFactor": 0.5,
                    "UL": "1",
                    "LL": "0",
                    "ExRT": "1" if ticker == "13010" else None,
                    "MktCap": 12345.5 if ticker == "13010" else None,
                }
                for ticker in ("13020", "13010")
            ]
        )
        payload = json.dumps({"data": rows}).encode()
        attempts.append(
            PriceAttempt(
                str(offset),
                day,
                AT,
                (JQuantsPage("daily_bars", {"date": day.isoformat()}, AT, payload),),
            )
        )
    owner.commit(attempts)
    failed_at = AT + timedelta(hours=1)
    owner.commit(
        [
            PriceAttempt(
                "failed",
                DAY,
                failed_at,
                (
                    JQuantsPage(
                        "daily_bars",
                        {"date": DAY.isoformat()},
                        failed_at,
                        b'{"data":[],"pagination_key":"next"}',
                    ),
                ),
            )
        ]
    )
    return store, owner.fixed()


def test_rebuild_preserves_matching_days_and_repairs_other_days(tmp_path, fixed_prices):
    store, fixed = fixed_prices
    results = []
    for state in ("cold", "warm"):
        market = market_store(tmp_path / f"{state}.sqlite")
        if state == "warm":
            with sqlite3.connect(market) as conn:
                conn.executemany(
                    "INSERT INTO jquants_daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    reversed(results[0][1]["jquants_daily_bars"]),
                )
                conn.execute(
                    "UPDATE jquants_daily_bars SET ex_right_type='incorrect' WHERE traded_at=?",
                    ((DAY + timedelta(days=2)).isoformat(),),
                )
                conn.execute(
                    "INSERT INTO jquants_daily_bars(ticker,traded_at) VALUES('1301','1990-01-01')"
                )
                conn.execute(
                    "UPDATE jquants_daily_bars SET ex_right_type=CAST(X'80' AS TEXT) "
                    "WHERE traded_at=? AND ticker='1301'",
                    ((DAY + timedelta(days=2)).isoformat(),),
                )
                conn.execute(
                    "INSERT INTO jquants_daily_bars(ticker,traded_at) VALUES('1301',CAST(X'80' AS TEXT))"
                )
                conn.execute(
                    "CREATE TRIGGER keep_matching_day BEFORE DELETE ON jquants_daily_bars "
                    f"WHEN OLD.traded_at='{DAY.isoformat()}' BEGIN SELECT RAISE(ABORT,'unchanged day'); END"
                )
        saved = price_native.model_input(
            store,
            fixed,
            as_of=DAY + timedelta(days=3),
            evaluated_at=AT + timedelta(hours=1),
            market=market,
        )
        with sqlite3.connect(market) as connection:
            states = read_price_day_states(
                connection, start=DAY, end=DAY + timedelta(days=3), tickers=("1301", "1302")
            )
        prices = read_price_bars(market, start=DAY, end=DAY + timedelta(days=3))
        assert len(prices) == len(states) == 2
        results.append((saved, rows_at(market), prices, states))
    assert len(results[0][1]["jquants_daily_bars"]) == 4
    assert results[0] == results[1]
    summary = results[0][0].descriptor["summary"]
    assert (
        summary["failed_days"] == summary["normal_empty_days"] == summary["unavailable_days"] == 1
    )
    assert summary["coverage_status"] == "partial"


@pytest.mark.parametrize("failure", ["duplicate", "coverage", "retain"])
def test_transfer_or_coverage_failure_rolls_back_all_replaced_tables(
    tmp_path, monkeypatch, fixed_prices, failure
):
    store, fixed = fixed_prices
    market = market_store(tmp_path / "market.sqlite", daily_bars=(("9999", DAY.isoformat(), 9, 1),))
    with sqlite3.connect(market) as connection:
        connection.execute(
            "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
            ("old", 0, "daily_bars", "{}", AT.isoformat(), DAY.isoformat(), b"old"),
        )
        price_native.record_source_coverage(
            connection,
            source="jquants_daily_bars",
            coverage_key="old",
            coverage_start=DAY.isoformat(),
            coverage_end=DAY.isoformat(),
            record_count=1,
            status="ok",
            fetched_at_utc=AT.isoformat(),
        )
    before = rows_at(market)
    store.put_keys.clear()
    if failure == "duplicate":
        rebuild = price_native.rebuild_price_days

        def duplicate(*args, **kwargs):
            result = rebuild(*args, **kwargs)
            last = DAY + timedelta(days=2)
            if last in result:
                item = result[last]
                result[last] = replace(item, rows=(*item.rows, item.rows[0]))
            return result

        monkeypatch.setattr(price_native, "rebuild_price_days", duplicate)
    elif failure == "retain":

        def fail_before_retain(*args, **kwargs):
            assert rows_at(market) == before  # Readers still see the unchanged generation.
            raise sqlite3.IntegrityError("retain failure")

        monkeypatch.setattr(price_native, "retain_price_input", fail_before_retain)
    else:
        record = price_native.record_source_coverage

        def fail_after_write(*args, **kwargs):
            record(*args, **kwargs)
            raise sqlite3.IntegrityError("coverage failure")

        monkeypatch.setattr(price_native, "record_source_coverage", fail_after_write)
    with pytest.raises(
        sqlite3.IntegrityError, match=r"UNIQUE constraint failed|coverage failure|retain failure"
    ):
        price_native.model_input(
            store,
            fixed,
            as_of=DAY + timedelta(days=3),
            evaluated_at=AT + timedelta(hours=1),
            market=market,
        )
    assert rows_at(market) == before
    assert not store.put_keys


def test_cold_price_rebuilds_reuse_verified_inputs_and_publish_the_marker_last(
    tmp_path, monkeypatch, fixed_prices
):
    store, fixed = fixed_prices
    end = DAY + timedelta(days=2)
    market = market_store(tmp_path / "market.sqlite")
    saved = price_native.model_input(
        store, fixed, as_of=end, evaluated_at=AT + timedelta(hours=1), market=market
    )
    days = (DAY, DAY + timedelta(days=1), end)
    selected = read_price_input(store, saved.descriptor, days=days)
    expected_rows = rows_at(market)["jquants_daily_bars"]
    cold_output, direct_output = tmp_path / "cold", tmp_path / "direct"
    committed = []
    rename = Path.rename

    def check_marker(path, target):
        if target.parent in (cold_output, direct_output) and target.name == "input.json":
            assert pq.ParquetFile(target.parent / "daily_bars.parquet").metadata.num_rows == 4
            assert (
                pq.ParquetFile(target.parent / "price_receipt_status.parquet").metadata.num_rows
                == 3
            )
            assert (target.parent / "head.json").exists() is (target.parent == direct_output)
            committed.append(target.parent)
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", check_marker)
    store.get_keys.clear()
    cold = price_native.rebuild_saved_input(store, saved, start=DAY, end=end, output=cold_output)
    assert Counter(key.split("/")[-2] for key in store.get_keys) == {
        "selections": 1,
        "months": 2,
        "packs": 3,
    }
    store.get_keys.clear()
    direct = price_native.rebuild(store, fixed, start=DAY, end=end, output=direct_output)
    assert Counter(key.split("/")[-2] for key in store.get_keys) == {"packs": 2}
    assert committed == [cold_output, direct_output]
    assert cold["rows"] == direct["rows"] == 4
    assert (direct_output / "head.json").read_bytes() == fixed.payload
    assert json.loads((cold_output / "input.json").read_bytes()) == {
        "saved_input": saved.model_dump(mode="json"),
        "source_head_sha256": fixed.content_id,
        "start": DAY.isoformat(),
        "end": end.isoformat(),
        **selected,
    }
    bars = pq.read_table(cold_output / "daily_bars.parquet")
    assert bars.equals(pq.read_table(direct_output / "daily_bars.parquet"))
    assert sorted(tuple(row.values()) for row in bars.to_pylist()) == sorted(expected_rows)
    status = pq.read_table(cold_output / "price_receipt_status.parquet")
    assert status.equals(pq.read_table(direct_output / "price_receipt_status.parquet"))


@pytest.mark.parametrize("saved_mode", [True, False])
def test_corrupt_price_pack_after_a_completed_batch_leaves_no_output(tmp_path, saved_mode):
    store = MemoryR2Store()
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    end = DAY + timedelta(days=1)
    keys = []
    for i, day in enumerate((DAY, end)):
        value = day.isoformat()
        body = json.dumps({"data": [{"Code": "13010", "Date": value, "C": 100 + i}]}).encode()
        owner.commit(
            [PriceAttempt(value, day, AT, (JQuantsPage("daily_bars", {"date": value}, AT, body),))]
        )
        head = owner.fixed().value()
        identity = head["scopes"][value]["adopted"]
        keys.append(PREFIX + "packs/" + head["receipts"][identity]["pack"] + ".json.gz")
    fixed = owner.fixed()
    saved = price_native.model_input(
        store, fixed, as_of=end, evaluated_at=AT, market=market_store(tmp_path / "market.sqlite")
    )
    store.values[keys[-1]].body = b"corrupt"
    store.get_keys.clear()
    output = tmp_path / "failed"
    run, selection = (
        (price_native.rebuild_saved_input, saved) if saved_mode else (price_native.rebuild, fixed)
    )
    with pytest.raises(LakePublishError, match="native L1 object failed integrity"):
        run(store, selection, start=DAY, end=end, output=output)
    assert _pack_reads(store) == keys
    assert not output.exists()
    assert list(tmp_path.glob(".price-rebuild-*")) == []


@pytest.mark.parametrize("claim_during_rebuild", [False, True])
def test_cold_rebuild_does_not_replace_an_empty_directory_even_if_claimed_late(
    tmp_path, monkeypatch, fixed_prices, claim_during_rebuild
):
    store, fixed = fixed_prices
    output = tmp_path / "occupied"
    if claim_during_rebuild:
        write = price_native.write_json_atomic

        def claim(path, payload):
            write(path, payload)
            output.mkdir()

        monkeypatch.setattr(price_native, "write_json_atomic", claim)
    else:
        output.mkdir()
    with pytest.raises(FileExistsError):
        price_native.rebuild(store, fixed, start=DAY, end=DAY, output=output)
    assert list(output.iterdir()) == []
    assert list(tmp_path.glob(".price-rebuild-*")) == []


def test_packing_cannot_trade_an_extra_read_of_one_original_for_two_other_reads():
    # The mixed correction pack A would be split twice by packing runs even though
    # the total drops from 7 GETs to 6. Its size is unknown, so keep monthly reads.
    packs = "D" * 18 + "A" * 9 + "C" * 8 + "A" * 10 + "E" * 19 + "B"
    days = tuple(date(2026, 10, 16) + timedelta(days=i) for i in range(len(packs)))
    scopes = {day.isoformat(): {"adopted": str(i)} for i, day in enumerate(days)}
    receipts = {str(i): {"pack": pack} for i, pack in enumerate(packs)}
    assert price_native._native_batches(days, scopes=scopes, receipts=receipts) == list(
        price_native._months(days)
    )


@pytest.mark.parametrize("entrypoint", ["model", "rebuild"])
def test_fixed_head_is_decoded_once_and_stays_local(
    tmp_path, monkeypatch, fixed_prices, entrypoint
):
    from baibai_engine.data_collection.providers.jquants.price_native import HEAD_KEY, FixedPriceL1
    from baibai_engine.storage.objects.r2 import LakePublishError

    store, fixed = fixed_prices
    original_value = FixedPriceL1.value
    original_read = price_native.read_price_requests
    decoded = []
    observed = []

    def value(self):
        result = original_value(self)
        decoded.append(result)
        return result

    def read(store, *, scopes, receipts, days, **kwargs):
        observed.append((scopes, receipts))
        # The mutable head can move without altering an operation's fixed bytes.
        store.values[HEAD_KEY].body = b'{"format":"changed"}'
        return original_read(store, scopes=scopes, receipts=receipts, days=days, **kwargs)

    monkeypatch.setattr(FixedPriceL1, "value", value)
    monkeypatch.setattr(price_native, "read_price_requests", read)
    if entrypoint == "model":
        result = price_native.model_input(
            store,
            fixed,
            as_of=DAY + timedelta(days=3),
            evaluated_at=AT + timedelta(hours=1),
            market=market_store(tmp_path / "reuse.sqlite"),
        )
        assert result.descriptor["summary"]["failed_days"] == 1
    else:
        result = price_native.rebuild(
            store,
            fixed,
            start=DAY,
            end=DAY + timedelta(days=3),
            output=tmp_path / "rebuilt",
        )
        assert result["rows"] == 4
        assert result["coverage_status"] == "partial"
    assert len(decoded) == 1
    assert len(observed) == 1  # The same native pack spans October and November.
    assert all(
        scopes is decoded[0]["scopes"] and receipts is decoded[0]["receipts"]
        for scopes, receipts in observed
    )
    # No cross-operation cache: a different invalid snapshot is checked afresh.
    invalid = json.loads(fixed.payload)
    invalid["pending"] = {"legacy": True}
    with pytest.raises(LakePublishError, match="pending"):
        price_native.rebuild(
            store,
            FixedPriceL1(json.dumps(invalid).encode(), "other"),
            start=DAY,
            end=DAY,
            output=tmp_path / "invalid",
        )
    assert not (tmp_path / "invalid").exists()


def test_daily_uses_verified_prices_but_disputed_and_large_requests_keep_cold_path(
    tmp_path, monkeypatch, fixed_prices
):
    from collections import Counter

    from baibai_engine.data_collection.providers.jquants import contracts
    from baibai_engine.data_collection.providers.jquants.price_native import PriceL1

    store, _ = fixed_prices
    owner = PriceL1(store, tmp_path / "native")
    huge = DAY + timedelta(days=3)
    plain = {"Code": "13010", "Date": huge.isoformat(), "C": 8}
    # One normal row but an unusual >4MiB native page.
    body = json.dumps({"data": [plain], "extra": "x" * (4 * 1024**2)}).encode()
    owner.commit(
        [
            PriceAttempt(
                "large",
                huge,
                AT,
                (JQuantsPage("daily_bars", {"date": huge.isoformat()}, AT, body),),
            )
        ]
    )
    fixed = owner.fixed()
    seen = Counter()
    page_envelope = contracts.page_envelope

    def counted(page):
        seen[(page.query["date"], page.body[:24])] += 1
        return page_envelope(page)

    monkeypatch.setattr(contracts, "page_envelope", counted)
    market = market_store(tmp_path / "prices.sqlite")
    saved = price_native.model_input(
        store, fixed, as_of=huge, evaluated_at=AT + timedelta(hours=1), market=market
    )
    by_day = Counter()
    for (day, _), n in seen.items():
        by_day[day] += n
    assert [by_day[(DAY + timedelta(days=i)).isoformat()] for i in range(4)] == [3, 1, 1, 2]
    assert saved.descriptor["summary"]["partial_days"] == 0
    assert len(rows_at(market)["jquants_daily_bars"]) == 5


def test_verified_rows_do_not_turn_discarded_conflicts_into_errors_or_cross_requests():
    from baibai_engine.data_collection.providers.jquants.contracts import (
        RetainedRequest,
        complete_request_with_records,
    )
    from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
    from baibai_engine.machine_analysis.observations.prices.from_l1 import (
        PriceBatchRows,
        rebuild_price_days,
    )

    def checked(values, at):
        body = json.dumps(
            {"data": [{"Code": "13010", "Date": DAY.isoformat(), "C": v} for v in values]}
        ).encode()
        return complete_request_with_records(
            (JQuantsPage("daily_bars", {"date": DAY.isoformat()}, at, body),)
        )

    early, early_rows = checked([100, 101], AT)
    late, late_rows = checked([200], AT + timedelta(seconds=1))
    state = PriceBatchRows()
    state.consume("first", early, early_rows)
    state.consume("second", late, late_rows)
    assert state.get("second", early) is None
    early_item = RetainedRequest("first", early, None)
    late_item = RetainedRequest("second", late, None)
    # A conflicting old price was not selected. Selecting it alone must still fail.
    for order in ((early_item, late_item), (late_item, early_item)):
        assert rebuild_price_days(order, days=(DAY,), prepared=state) == rebuild_price_days(
            order, days=(DAY,)
        )
    with pytest.raises(JQuantsProviderError, match="conflicting daily bars"):
        rebuild_price_days((early_item,), days=(DAY,), prepared=state)


def _fixed_pack_boundaries(store, tmp_path):
    # Distinct packs can span a month boundary or contain nonadjacent
    # correction dates, so the existing planner must use three month batches.
    pack_ids = "D" * 18 + "A" * 9 + "C" * 8 + "A" * 10 + "E" * 19 + "B"
    start = date(2026, 10, 16)
    end = start + timedelta(days=len(pack_ids) - 1)
    stamp = datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC)
    owner = PriceL1(store, tmp_path / "native")
    owner.initialize()
    groups = {}
    for i, tag in enumerate(pack_ids):
        day = (start + timedelta(days=i)).isoformat()
        rows = (
            []
            if i == 20
            else [
                {
                    "Code": "13010",
                    "Date": day,
                    "O": 101 + i,
                    "H": 105 + i,
                    "L": 99 + i,
                    "C": 103 + i,
                    "Vo": 1234,
                    "Va": 127102,
                    "AdjFactor": 0.5,
                    "UL": "1",
                    "LL": "0",
                    "ExRT": "1",
                    "MktCap": 12345.5,
                }
            ]
        )
        groups.setdefault(tag, []).append(
            PriceAttempt(
                str(i),
                date.fromisoformat(day),
                stamp,
                (
                    JQuantsPage(
                        "daily_bars", {"date": day}, stamp, json.dumps({"data": rows}).encode()
                    ),
                ),
            )
        )
    for attempts in groups.values():
        owner.commit(attempts)
    return owner.fixed(), end, stamp


def _pack_reads(store):
    return [key for key in store.get_keys if key.startswith(PREFIX + "packs/")]


def test_daily_pack_prefetch_preserves_fixed_input_sqlite_and_parquet_bytes(tmp_path, monkeypatch):
    store = MemoryR2Store()
    fixed, day, stamp = _fixed_pack_boundaries(store, tmp_path)
    raw = {
        key: hashlib.sha256(value.body).hexdigest()
        for key, value in store.values.items()
        if key.startswith(PREFIX)
    }
    results = []
    bounded_reads = []
    get = store.get_bytes_with_metadata

    def read_bounded(key, *, sha256, max_bytes):
        assert key == PREFIX + "packs/" + sha256 + ".json.gz"
        assert max_bytes == MAX_PACK_BYTES + 128 * 1024
        bounded_reads.append(key)
        loaded = get(key)
        assert loaded is not None
        assert 0 < len(loaded[0]) <= max_bytes
        return loaded[0]

    monkeypatch.setattr(store, "get_verified_bytes", read_bounded, raising=False)
    for kind in ("serial", "prefetch"):
        store.get_keys.clear()
        market = market_store(tmp_path / (kind + ".sqlite"))
        with monkeypatch.context() as options:
            if kind == "serial":
                options.setattr(price_native, "prefetch_price_packs", lambda *a, **k: nullcontext())
            identity = price_native.model_input(
                store, fixed, as_of=day, evaluated_at=stamp, market=market
            )
        mirror = tmp_path / kind
        with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=mirror) as snapshot:
            report = export_prices_from_native_input(
                source_snapshot=snapshot,
                saved=identity,
                mirror_root=mirror,
                producer_git_commit="f" * 40,
                build_id="fixed",
                created_at=stamp,
            )
        results.append((identity.model_dump_json(), rows_at(market), report.manifest.partitions))
        keys = _pack_reads(store)
        assert len(keys) == (7 if kind == "serial" else 5)
        assert set(keys) == set(raw) - {PREFIX + "head.json"}
    assert results[0] == results[1]
    assert len(bounded_reads) == 5
    assert set(bounded_reads) == set(raw) - {PREFIX + "head.json"}
    assert {key: hashlib.sha256(store.values[key].body).hexdigest() for key in raw} == raw


@pytest.mark.parametrize("failure", ["missing", "corrupt", "refused"])
def test_prefetch_bounded_at_first_failed_pack_and_rolls_back(tmp_path, monkeypatch, failure):
    store = MemoryR2Store()
    fixed, day, stamp = _fixed_pack_boundaries(store, tmp_path)
    price = fixed.value()
    first_id = price["scopes"][min(price["scopes"])]["adopted"]
    bad = PREFIX + "packs/" + price["receipts"][first_id]["pack"] + ".json.gz"
    first_group = Barrier(3, timeout=3)
    lock = Lock()
    touched = []
    read = store.get_bytes_with_metadata

    def fail(key):
        if not key.startswith(PREFIX + "packs/"):
            return read(key)
        with lock:
            touched.append(key)
            if len(touched) > 3:
                pytest.fail("scheduled beyond the initial lookahead after failure")
        first_group.wait()
        if key == bad:
            if failure == "missing":
                return None
            if failure == "refused":
                raise R2Error("fixed first request rejected")
            changed = b"wrong original bytes"
            remote = store.head(key)
            assert remote is not None
            return changed, replace(
                remote, size=len(changed), metadata={"sha256": hashlib.sha256(changed).hexdigest()}
            )
        # An independently broken later pack must not mask the leftmost failure.
        raise R2Error("subsequent pack rejected")

    monkeypatch.setattr(store, "get_bytes_with_metadata", fail)
    market = market_store(tmp_path / "market.sqlite", daily_bars=(("9999", DAY.isoformat(), 9, 1),))
    before = rows_at(market)
    store.put_keys.clear()
    error = R2Error if failure == "refused" else LakePublishError
    label = "fixed first request rejected" if failure == "refused" else "fixed full-day L1 pack"
    with pytest.raises(error, match=label):
        price_native.model_input(store, fixed, as_of=day, evaluated_at=stamp, market=market)
    assert len(touched) == len(set(touched)) == 3
    assert bad in touched
    assert rows_at(market) == before
    assert not store.put_keys
