"""外部評価の固定universeと保存原本readbackを使う合成入力。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    write_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from tests.helpers.master import TSE_33_SECTORS, insert_master_rows
from tests.helpers.r2_store import MemoryR2Store

ASOF = date(2026, 10, 11)
RECEIVED = datetime(2026, 10, 11, 0, 0, 1, 123456, tzinfo=UTC)
ORIGIN = LakeStoreOrigin("synthetic-fixed-release", "a" * 64)


def seed_database(path, *, count=2500, snapshot=ASOF, origin=True):
    sectors = sorted(TSE_33_SECTORS)
    markets = ("PRIME", "standard", "グロース", "東証一部")
    tickers = tuple(str(1000 + index) for index in range(count))
    connection = open_connection(path)
    try:
        insert_master_rows(
            connection,
            [
                (
                    snapshot.isoformat(),
                    ticker,
                    f"合成会社{index}",
                    markets[index % 4],
                    sectors[index % 33],
                    "011",
                )
                for index, ticker in enumerate(tickers)
            ],
        )
        if origin:
            write_lake_store_origin(connection, ORIGIN)
        connection.execute(
            "INSERT INTO source_coverage(source,coverage_key,fetched_at_utc,record_count,status) VALUES (?,?,?,?,?)",
            ("synthetic", "saved", RECEIVED.isoformat(), count, "ok"),
        )
        connection.commit()
    finally:
        connection.close()
    return tickers


def build_native_master(tmp_path, monkeypatch, *, snapshot=ASOF, count=2500, calendar=None):
    """実native原本から固定universeを作る。旧lakeもready依存も用意しない。"""
    import json

    from baibai_engine.data_collection.providers.jquants.calendar_native import (
        CalendarAttempt,
        CalendarL1,
    )
    from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
    from baibai_engine.data_collection.providers.jquants.master_native import (
        MasterAttempt,
        MasterL1,
    )

    database = tmp_path / "market.sqlite"
    seed_database(database, snapshot=snapshot, count=count)
    with closing(sqlite3.connect(database)) as connection, connection:
        insert_master_rows(
            connection,
            [
                (snapshot.isoformat(), "130A", "合成英字会社", "PRIME", "電気機器", "011"),
                (snapshot.isoformat(), "9401", "合成ETF", "PRIME", "その他", "014"),
                (snapshot.isoformat(), "9402", "合成PRO", "TOKYO PRO MARKET", "電気機器", "011"),
                (snapshot.isoformat(), "9403", "合成市場外", "RESIDUAL", "電気機器", "011"),
            ],
        )
        rows = [
            json.loads(payload)
            for (payload,) in connection.execute(
                "SELECT record_json FROM jquants_master_snapshots ORDER BY security_code"
            )
        ]
    store = MemoryR2Store()
    master = MasterL1(store, tmp_path / "master-native")
    master.initialize()
    master.commit(
        [
            MasterAttempt(
                "fixture-master",
                snapshot,
                RECEIVED,
                (
                    JQuantsPage(
                        "master",
                        {"date": snapshot.isoformat()},
                        RECEIVED,
                        json.dumps({"data": rows}).encode(),
                    ),
                ),
            )
        ]
    )
    if calendar:
        first, last = min(day for day, _ in calendar), max(day for day, _ in calendar)
        owner = CalendarL1(store, tmp_path / "calendar-native")
        owner.initialize()
        owner.commit(
            [
                CalendarAttempt(
                    "fixture-calendar",
                    date.fromisoformat(first),
                    date.fromisoformat(last),
                    RECEIVED,
                    (
                        JQuantsPage(
                            "market_calendar",
                            {"from": first, "to": last},
                            RECEIVED,
                            json.dumps(
                                {
                                    "data": [
                                        {"Date": day, "HolDiv": str(division)}
                                        for day, division in calendar
                                    ]
                                }
                            ).encode(),
                        ),
                    ),
                )
            ]
        )
    store.get_keys.clear()
    store.put_keys.clear()
    return store, master, database
