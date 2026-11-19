"""SQLite転送の順序を変えても全値・固定入力・coverageと原子性を保つ。"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.price_native import PriceAttempt, PriceL1
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.machine_analysis.observations.prices.price_observation_reader import (
    read_price_day_states,
)
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_jobs.integration import price_native

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


def test_native_price_paths_use_small_runner_session(tmp_path, monkeypatch, fixed_prices):
    from pathlib import Path

    monkeypatch.setenv("BAIBAI_LAKE_QUERY_PROFILE", "small-runner")
    observed = []

    @contextmanager
    def recorded_session():
        with lake_session() as session:
            settings = dict(
                session.connection.execute(
                    "SELECT name,value FROM duckdb_settings() WHERE name IN "
                    "('memory_limit','threads','max_temp_directory_size','temp_directory',"
                    "'autoload_known_extensions','autoinstall_known_extensions')"
                ).fetchall()
            )
            observed.append(settings)
            assert Path(settings["temp_directory"]).is_dir()
            yield session

    monkeypatch.setattr(price_native, "lake_session", recorded_session)
    store, fixed = fixed_prices
    end = DAY + timedelta(days=2)
    market = market_store(tmp_path / "limited.sqlite")
    saved = price_native.model_input(
        store, fixed, as_of=end, evaluated_at=AT + timedelta(hours=1), market=market
    )
    cold = price_native.rebuild_saved_input(
        store, saved, start=DAY, end=end, output=tmp_path / "cold"
    )
    direct = price_native.rebuild(store, fixed, start=DAY, end=end, output=tmp_path / "direct")
    assert cold["rows"] == direct["rows"] == 4
    with sqlite3.connect(market) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone() == (4,)
    assert len(observed) == 2  # Only the two standalone Parquet builders need DuckDB.
    assert len({item["temp_directory"] for item in observed}) == len(observed)
    for settings in observed:
        assert settings["memory_limit"] == "953.6 MiB"
        assert settings["threads"] == "2"
        assert settings["max_temp_directory_size"] == "3.7 GiB"
        assert settings["autoload_known_extensions"] == "false"
        assert settings["autoinstall_known_extensions"] == "false"
        assert not Path(settings["temp_directory"]).exists()


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
