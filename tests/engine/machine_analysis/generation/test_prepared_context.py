"""生成内共有でもsplit、exact端点、自己除外、欠測の意味を変えない。"""

from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest
from tests.helpers.master import insert_master_rows
from tests.helpers.network import _no_real_network as _no_real_network
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.machine_analysis.generation.runners.market_snapshot import (
    build_market_snapshot,
    prepare_market_context_from_bars,
)
from baibai_engine.machine_analysis.generation.runners.security_context import (
    build_security_market_contexts,
)
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.stocks.reader import (
    read_master_snapshot,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

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
        insert_master_rows(
            connection,
            [
                ("2026-05-28", ticker, ticker, "プライム", "機械", "011")
                for ticker in ("AAAA", "BBBB", "NEW1", "OLD1", "NONE")
            ],
        )
        # Non-common securities are not industry peers even when the sector label matches.
        insert_master_rows(
            connection, [("2026-05-28", "PREF", "non-common security", "プライム", "機械", "014")]
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


def _shared_inputs(path: Path, tickers: tuple[str, ...]):
    bars = read_price_bars(path, start=date.min, end=ASOF)
    prepared = prepare_market_context_from_bars(
        sqlite_path=path,
        asof_date=ASOF,
        master=read_master_snapshot(path, as_of=ASOF),
        bars_by_ticker=bars,
        subjects=tickers,
    )
    return bars, prepared


def test_shared_generation_preserves_cli_market_and_adjusted_security_facts(context_store):
    tickers = ("AAAA", "BBBB", "NEW1", "OLD1", "NONE", "ZZZZ")
    bars, prepared = _shared_inputs(context_store, tickers)
    assert build_market_snapshot(
        sqlite_path=context_store, asof_date=ASOF
    ) == build_market_snapshot(sqlite_path=context_store, asof_date=ASOF, prepared=prepared)
    batch = build_security_market_contexts(
        sqlite_path=context_store,
        tickers=tickers,
        asof_date=ASOF,
        self_range_window_bars=750,
        prepared=prepared,
        price_bars_by_ticker=bars,
    )
    current = batch["AAAA"]
    assert current["price"]["resolved_date"] == "2026-05-29"
    assert current["price"]["close"] == 224.5
    assert current["master"]["snapshot_date"] == "2026-05-28"
    assert current["relative"]["sector"] is None  # no complete other peer endpoints
    assert batch["NEW1"]["price"]["bar_count"] == 3
    assert batch["OLD1"]["price"]["bar_count"] == 80
    assert batch["NONE"]["price"] is None
    assert batch["ZZZZ"]["master"] is None


def test_sector_candidates_keep_common_peers_and_each_tickers_exact_endpoints(
    context_store: Path,
) -> None:
    insert_daily_bars_from_closes(
        context_store,
        "CCCC",
        [100 + index**2 for index in range(350)],
        end_date=ASOF - timedelta(days=1),
    )
    insert_daily_bars_from_closes(
        context_store, "DDDD", [100] * 350, end_date=ASOF - timedelta(days=1)
    )
    insert_daily_bars_from_closes(
        context_store,
        "OLD2",
        [100 + index for index in range(80)],
        end_date=ASOF - timedelta(days=500),
    )
    with closing(open_connection(context_store)) as connection:
        insert_master_rows(
            connection,
            [
                ("2026-05-28", "CCCC", "peer", "プライム", "機械", "011"),
                ("2026-05-28", "DDDD", "other sector", "プライム", "情報・通信業", "011"),
                ("2026-05-28", "OLD2", "old peer", "プライム", "機械", "011"),
            ],
        )
        connection.commit()
    tickers = ("AAAA", "BBBB", "DDDD", "OLD1")
    bars, prepared = _shared_inputs(context_store, tickers)
    assert "PREF" not in prepared.common_stock_tickers_by_sector["機械"]
    assert prepared.common_stock_tickers_by_sector["情報・通信業"] == ("DDDD",)
    batch = build_security_market_contexts(
        sqlite_path=context_store,
        tickers=tickers,
        asof_date=ASOF,
        self_range_window_bars=750,
        prepared=prepared,
        price_bars_by_ticker=bars,
    )
    assert batch["DDDD"]["relative"]["sector"] is None  # other sector has no peers
    # BBBB lacks AAAA's comparison day; OLD1 uses older endpoints from the shared input.
    for ticker, start_days_ago, peer_count in (("AAAA", 21, 1), ("BBBB", 22, 2), ("OLD1", 520, 1)):
        sector = batch[ticker]["relative"]["sector"]
        assert sector["peer_count"] == peer_count
        assert (
            sector["comparison_start_date"] == (ASOF - timedelta(days=start_days_ago)).isoformat()
        )
        if ticker == "AAAA":
            assert sector["peer_median_return_20d"] == pytest.approx(
                (100 + 349**2) / (100 + 329**2) - 1
            )


def test_prepared_scope_cannot_change_dates(context_store: Path) -> None:
    bars, prepared = _shared_inputs(context_store, ("AAAA",))
    with pytest.raises(ValueError, match="scope"):
        build_security_market_contexts(
            sqlite_path=context_store,
            tickers=("AAAA",),
            asof_date=ASOF - timedelta(days=1),
            self_range_window_bars=750,
            prepared=prepared,
            price_bars_by_ticker=bars,
        )


@pytest.mark.parametrize("create_empty", [False, True])
def test_shared_unwritten_store_keeps_unknown_and_null(tmp_path: Path, create_empty: bool) -> None:
    path = tmp_path / "unwritten.sqlite"
    if create_empty:
        path.touch()
    bars, prepared = _shared_inputs(path, ("AAAA",))
    assert prepared.series_by_ticker == {}
    assert prepared.benchmark_trend is None
    assert build_security_market_contexts(
        sqlite_path=path,
        tickers=("AAAA",),
        asof_date=ASOF,
        self_range_window_bars=750,
        prepared=prepared,
        price_bars_by_ticker=bars,
    )["AAAA"] == {"master": None, "price": None, "relative": None, "benchmark_trend": None}
    snapshot = build_market_snapshot(sqlite_path=path, asof_date=ASOF, prepared=prepared)
    assert snapshot["sectors"] == []
    assert all(point["benchmark_trend"] == "unknown" for point in snapshot["points"])


def test_shared_bar_input_never_requeries_sparse_benchmark_or_peers(context_store, monkeypatch):
    insert_daily_bars_from_closes(
        context_store,
        "OLD2",
        [100 + index for index in range(80)],
        end_date=ASOF - timedelta(days=500),
    )
    with closing(open_connection(context_store)) as connection:
        insert_master_rows(
            connection, [("2026-05-28", "OLD2", "old peer", "プライム", "機械", "011")]
        )
        connection.commit()
    tickers = ("AAAA", "OLD1", "NEW1", "NONE")
    bars = read_price_bars(context_store, start=date.min, end=ASOF)
    master = read_master_snapshot(context_store, as_of=ASOF)

    def forbidden(*args, **kwargs):
        raise AssertionError("generation must reuse its shared price and master input")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    prepared = prepare_market_context_from_bars(
        sqlite_path=context_store,
        asof_date=ASOF,
        master=master,
        bars_by_ticker=bars,
        subjects=tickers,
    )
    contexts = build_security_market_contexts(
        sqlite_path=context_store,
        tickers=tickers,
        asof_date=ASOF,
        self_range_window_bars=750,
        prepared=prepared,
        price_bars_by_ticker=bars,
    )
    old = contexts["OLD1"]
    assert old["relative"]["sector"]["peer_count"] == 1
    assert old["relative"]["sector"]["relative_return_20d"] == 0
    assert old["relative"]["relative_20d"] is None
    assert contexts["AAAA"]["price"]["close"] == 224.5
    assert contexts["NEW1"]["price"]["bar_count"] == 3
    assert contexts["NONE"]["price"] is None


def test_common_market_window_does_not_expand_with_sparse_selected_subjects(context_store):
    old = [PriceBar(ASOF - timedelta(days=600 - i), 100.0 + i, None) for i in range(80)]
    bars = {"1321": old, "OLD1": old}
    master = read_master_snapshot(context_store, as_of=ASOF)
    snapshots = []
    for subjects in ((), ("OLD1",)):
        prepared = prepare_market_context_from_bars(
            sqlite_path=context_store,
            asof_date=ASOF,
            master=master,
            bars_by_ticker=bars,
            subjects=subjects,
        )
        snapshots.append(
            build_market_snapshot(sqlite_path=context_store, asof_date=ASOF, prepared=prepared)
        )
    assert snapshots[0] == snapshots[1]
    assert all(point["benchmark_return_20d"] is None for point in snapshots[1]["points"])


@pytest.mark.parametrize("mismatch", ["store", "history_weeks", "benchmark_ticker"])
def test_prepared_market_scope_rejects_a_different_request(context_store, mismatch):
    _, prepared = _shared_inputs(context_store, ("AAAA",))
    arguments = {"sqlite_path": context_store, "asof_date": ASOF, "prepared": prepared}
    if mismatch == "store":
        arguments["sqlite_path"] = context_store.with_name("another.sqlite")
    elif mismatch == "history_weeks":
        arguments["history_weeks"] = prepared.history_weeks + 1
    else:
        arguments["benchmark_ticker"] = "OTHER"
    with pytest.raises(ValueError, match=r"prepared market context.*scope"):
        build_market_snapshot(**arguments)


def test_preparation_resolves_store_scope_once(context_store, monkeypatch):
    bars = read_price_bars(context_store, start=date.min, end=ASOF)
    master = read_master_snapshot(context_store, as_of=ASOF)
    monkeypatch.chdir(context_store.parent)
    prepared = prepare_market_context_from_bars(
        sqlite_path=Path(context_store.name),
        asof_date=ASOF,
        master=master,
        bars_by_ticker=bars,
        subjects=("AAAA",),
    )
    assert prepared.sqlite_path == context_store.resolve()
    assert build_market_snapshot(
        sqlite_path=context_store, asof_date=ASOF, prepared=prepared
    ) == build_market_snapshot(sqlite_path=context_store, asof_date=ASOF)
