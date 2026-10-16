"""生成内共有でもsplit、exact端点、自己除外、欠測の意味を変えない。"""

from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.market_context.master import read_master_snapshot
from baibai_engine.market_context.security import build_security_market_context
from baibai_engine.market_context.snapshot import (
    build_market_snapshot,
    prepare_market_context_inputs,
)
from baibai_engine.storage.observations.schema import open_connection

ASOF = date(2026, 5, 30)


@pytest.fixture
def context_store(tmp_path: Path) -> Path:
    path = tmp_path / "market.sqlite"
    for ticker, count, end in (
        ("1321", 350, ASOF - timedelta(days=1)),
        ("AAAA", 350, ASOF - timedelta(days=1)),
        ("BBBB", 350, ASOF - timedelta(days=1)),
        ("PREF", 350, ASOF - timedelta(days=1)),
        ("NEW1", 3, ASOF - timedelta(days=1)),
        ("OLD1", 80, ASOF - timedelta(days=500)),
    ):
        insert_daily_bars_from_closes(
            path, ticker, [100 + index for index in range(count)], end_date=end
        )
    with closing(open_connection(path)) as connection:
        connection.executemany(
            "INSERT INTO jquants_master_snapshots "
            "(snapshot_date,ticker,name,market,sector_33,is_common_stock) "
            "VALUES ('2026-05-28',?,?,'プライム','機械',1)",
            [(ticker, ticker) for ticker in ("AAAA", "BBBB", "NEW1", "OLD1", "NONE")],
        )
        # Non-common securities are not industry peers even when the sector label matches.
        connection.execute(
            "INSERT INTO jquants_master_snapshots "
            "(snapshot_date,ticker,name,market,sector_33,is_common_stock) "
            "VALUES ('2026-05-28','PREF','non-common security','プライム','機械',0)"
        )
        # An observed split without a close must still normalize the previous price.
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close,adjustment_factor) "
            "VALUES ('AAAA','2026-05-30',NULL,0.5)"
        )
        # Peer endpoints are exact: do not fill BBBB's missing comparison date.
        connection.execute(
            "DELETE FROM jquants_daily_bars WHERE ticker='BBBB' AND traded_at='2026-05-09'"
        )
        connection.commit()
    return path


def test_shared_generation_preserves_market_and_all_security_outputs(context_store: Path) -> None:
    prepared = prepare_market_context_inputs(
        sqlite_path=context_store,
        asof_date=ASOF,
        master=read_master_snapshot(context_store, as_of=ASOF),
    )
    assert build_market_snapshot(
        sqlite_path=context_store, asof_date=ASOF
    ) == build_market_snapshot(sqlite_path=context_store, asof_date=ASOF, prepared=prepared)
    for ticker in ("AAAA", "BBBB", "NEW1", "OLD1", "NONE", "ZZZZ"):
        arguments = dict(
            sqlite_path=context_store, ticker=ticker, asof_date=ASOF, self_range_window_bars=750
        )
        plain = build_security_market_context(**arguments)
        shared = build_security_market_context(**arguments, prepared=prepared)
        assert plain == shared
    current = build_security_market_context(
        sqlite_path=context_store,
        ticker="AAAA",
        asof_date=ASOF,
        self_range_window_bars=750,
        prepared=prepared,
    )
    assert current["price"]["resolved_date"] == "2026-05-29"
    assert current["price"]["close"] == 224.5
    assert current["master"]["snapshot_date"] == "2026-05-28"
    assert current["relative"]["sector"] is None  # no complete other peer endpoints


def test_shared_security_reads_only_its_long_history(context_store: Path, mocker) -> None:
    prepared = prepare_market_context_inputs(
        sqlite_path=context_store,
        asof_date=ASOF,
        master=read_master_snapshot(context_store, as_of=ASOF),
    )
    read = mocker.spy(
        __import__("baibai_engine.market_context.security", fromlist=["read_price_bars"]),
        "read_price_bars",
    )
    for ticker in ("AAAA", "BBBB"):
        build_security_market_context(
            sqlite_path=context_store,
            ticker=ticker,
            asof_date=ASOF,
            self_range_window_bars=750,
            prepared=prepared,
        )
    assert [call.kwargs["tickers"] for call in read.call_args_list] == [("AAAA",), ("BBBB",)]


def test_prepared_scope_cannot_change_dates(context_store: Path) -> None:
    prepared = prepare_market_context_inputs(
        sqlite_path=context_store,
        asof_date=ASOF,
        master=read_master_snapshot(context_store, as_of=ASOF),
    )
    with pytest.raises(ValueError, match="scope"):
        build_security_market_context(
            sqlite_path=context_store,
            ticker="AAAA",
            asof_date=ASOF - timedelta(days=1),
            self_range_window_bars=750,
            prepared=prepared,
        )


@pytest.mark.parametrize("create_empty", [False, True])
def test_shared_unwritten_store_keeps_unknown_and_null(tmp_path: Path, create_empty: bool) -> None:
    path = tmp_path / "unwritten.sqlite"
    if create_empty:
        path.touch()
    prepared = prepare_market_context_inputs(sqlite_path=path, asof_date=ASOF, master=None)
    assert prepared.series_by_ticker == {}
    assert prepared.benchmark_trend is None
    assert build_security_market_context(
        sqlite_path=path,
        ticker="AAAA",
        asof_date=ASOF,
        self_range_window_bars=750,
        prepared=prepared,
    ) == {"master": None, "price": None, "relative": None, "benchmark_trend": None}
    snapshot = build_market_snapshot(sqlite_path=path, asof_date=ASOF, prepared=prepared)
    assert snapshot["sectors"] == []
    assert all(point["benchmark_trend"] == "unknown" for point in snapshot["points"])
