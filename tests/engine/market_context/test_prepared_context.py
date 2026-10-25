"""生成内共有でもsplit、exact端点、自己除外、欠測の意味を変えない。"""

from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
from tests.helpers.master import insert_master_rows
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.machine_analysis.market_context.security import (
    build_security_market_context,
    build_security_market_contexts,
)
from baibai_engine.machine_analysis.market_context.snapshot import (
    build_market_snapshot,
    prepare_market_context_from_bars,
    prepare_market_context_inputs,
)
from baibai_engine.market.adjusted_prices import read_price_bars
from baibai_engine.market.jquants_reader import read_master_snapshot
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


def test_shared_generation_preserves_market_and_all_security_outputs(context_store: Path) -> None:
    prepared = prepare_market_context_inputs(
        sqlite_path=context_store,
        asof_date=ASOF,
        master=read_master_snapshot(context_store, as_of=ASOF),
    )
    assert build_market_snapshot(
        sqlite_path=context_store, asof_date=ASOF
    ) == build_market_snapshot(sqlite_path=context_store, asof_date=ASOF, prepared=prepared)
    tickers = ("AAAA", "BBBB", "NEW1", "OLD1", "NONE", "ZZZZ")
    batch = build_security_market_contexts(
        sqlite_path=context_store,
        tickers=tickers,
        asof_date=ASOF,
        self_range_window_bars=750,
        prepared=prepared,
    )
    for ticker in tickers:
        arguments = dict(
            sqlite_path=context_store, ticker=ticker, asof_date=ASOF, self_range_window_bars=750
        )
        plain = build_security_market_context(**arguments)
        shared = build_security_market_context(**arguments, prepared=prepared)
        assert plain == shared == batch[ticker]
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
    master = read_master_snapshot(context_store, as_of=ASOF)
    assert master is not None
    securities = mocker.Mock(wraps=master.securities)
    prepared = prepare_market_context_inputs(
        sqlite_path=context_store,
        asof_date=ASOF,
        master=replace(master, securities=securities),
    )
    assert securities.items.call_count == 1
    read = mocker.spy(
        __import__(
            "baibai_engine.machine_analysis.market_context.security", fromlist=["read_price_bars"]
        ),
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
    assert securities.items.call_count == 1


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
    prepared = prepare_market_context_inputs(
        sqlite_path=context_store,
        asof_date=ASOF,
        master=read_master_snapshot(context_store, as_of=ASOF),
    )
    assert "PREF" not in prepared.common_stock_tickers_by_sector["機械"]
    assert prepared.common_stock_tickers_by_sector["情報・通信業"] == ("DDDD",)
    batch = build_security_market_contexts(
        sqlite_path=context_store,
        tickers=("AAAA", "BBBB", "DDDD", "OLD1"),
        asof_date=ASOF,
        self_range_window_bars=750,
        prepared=prepared,
    )
    assert batch["DDDD"]["relative"]["sector"] is None  # other sector has no peers
    # BBBB lacks AAAA's comparison day; OLD1 needs the existing long-history fallback.
    for ticker, start_days_ago, peer_count in (("AAAA", 21, 1), ("BBBB", 22, 2), ("OLD1", 520, 1)):
        arguments = dict(
            sqlite_path=context_store, ticker=ticker, asof_date=ASOF, self_range_window_bars=750
        )
        plain = build_security_market_context(**arguments)
        shared = build_security_market_context(**arguments, prepared=prepared)
        assert shared == plain == batch[ticker]
        sector = shared["relative"]["sector"]
        assert sector["peer_count"] == peer_count
        assert (
            sector["comparison_start_date"] == (ASOF - timedelta(days=start_days_ago)).isoformat()
        )
        if ticker == "AAAA":
            assert sector["peer_median_return_20d"] == pytest.approx(
                (100 + 349**2) / (100 + 329**2) - 1
            )


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
    expected = build_security_market_contexts(
        sqlite_path=context_store, tickers=tickers, asof_date=ASOF, self_range_window_bars=750
    )
    assert expected["OLD1"]["relative"]["sector"]["peer_count"] == 1
    bars = read_price_bars(context_store, start=date.min, end=ASOF)
    master = read_master_snapshot(context_store, as_of=ASOF)

    def forbidden(*args, **kwargs):
        raise AssertionError("generation must reuse its shared price input")

    monkeypatch.setattr(
        "baibai_engine.machine_analysis.market_context.snapshot.read_price_bars", forbidden
    )
    monkeypatch.setattr(
        "baibai_engine.machine_analysis.market_context.security.read_price_bars", forbidden
    )
    prepared = prepare_market_context_from_bars(
        sqlite_path=context_store,
        asof_date=ASOF,
        master=master,
        bars_by_ticker=bars,
        subjects=tickers,
    )
    assert build_security_market_contexts(
        sqlite_path=context_store,
        tickers=tickers,
        asof_date=ASOF,
        self_range_window_bars=750,
        prepared=prepared,
        price_bars_by_ticker=bars,
    ) == expected
