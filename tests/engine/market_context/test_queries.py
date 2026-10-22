"""Market Contextの過去分類、統計量・端点、保存読取とimportの境界。"""

import hashlib
import socket
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from functools import partial

import pytest
from tests.helpers.assessment_history import historical_context_payload, seed_history
from tests.helpers.master import insert_master_rows
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.collection.economic.store import initialize_database
from baibai_engine.market_context.benchmark import compute_benchmark_trend
from baibai_engine.market_context.master import read_master_snapshot
from baibai_engine.market_context.prices import PriceBar, read_price_bars
from baibai_engine.market_context.reading.query import reading_snapshot
from baibai_engine.market_context.sector import _rank_to_percentiles, sector_relative_strength
from baibai_engine.market_context.security import _bollinger_20bar, build_security_market_context
from baibai_engine.market_context.snapshot import build_market_snapshot
from baibai_engine.research.market_context.models import StoredAssessment
from baibai_engine.research.market_context.query import assessment_payload as read_context
from baibai_engine.research.market_context.query import latest_assessment_payload
from baibai_engine.storage.observations.schema import open_connection

ASOF = date(2026, 5, 29)


def master(path, snapshot, memberships):
    connection = open_connection(path)
    try:
        insert_master_rows(
            connection,
            [
                (
                    snapshot,
                    ticker,
                    ticker,
                    "プライム",
                    sector,
                    "014" if sector == "その他" else "011",
                )
                for ticker, sector in memberships.items()
            ],
        )
        connection.commit()
    finally:
        connection.close()


def bars(path, ticker, total_return, *, end=ASOF):
    insert_daily_bars_from_closes(
        path, ticker, [100] * 20 + [100 * (1 + total_return)], end_date=end
    )


def security(path, *, ticker="AAAA", as_of=ASOF):
    return build_security_market_context(
        sqlite_path=path, ticker=ticker, asof_date=as_of, self_range_window_bars=750
    )


def test_past_master_and_peers_ignore_future_classification_and_prices(tmp_path):
    path = tmp_path / "market.sqlite"
    for ticker, ret in (("AAAA", -0.1), ("BBBB", 0.2), ("CCCC", 0.4), ("DDDD", 1), ("1321", 0.1)):
        bars(path, ticker, ret)
    master(path, "2026-05-28", {"AAAA": "機械", "BBBB": "機械", "CCCC": "機械", "DDDD": "小売業"})
    master(path, "2026-06-01", {"AAAA": "小売業", "BBBB": "建設業", "DDDD": "小売業"})
    result = security(path)
    assert result["master"]["sector_33"] == "機械"
    assert result["master"]["snapshot_date"] == "2026-05-28"
    peer = result["relative"]["sector"]
    assert peer["peer_count"] == 2
    assert peer["peer_median_return_20d"] == pytest.approx(0.3)
    assert peer["relative_return_20d"] == pytest.approx(-0.4)
    assert peer["comparison_start_date"] < peer["comparison_end_date"] == ASOF.isoformat()
    assert result["relative"]["relative_20d"] == pytest.approx(-0.2)
    assert (
        build_market_snapshot(sqlite_path=path, asof_date=ASOF)["master_snapshot_date"]
        == "2026-05-28"
    )
    assert security(path, as_of=date(2026, 6, 1))["master"]["sector_33"] == "小売業"


def test_latest_eligible_snapshot_does_not_restore_removed_members(tmp_path):
    path = tmp_path / "market.sqlite"
    bars(path, "AAAA", 0.1)
    master(path, "2026-05-27", {"AAAA": "機械"})
    master(path, "2026-05-28", {"BBBB": "機械"})
    result = security(path)
    assert result["master"] is None
    assert result["price"]["close"] == pytest.approx(110)
    assert result["relative"]["sector"] is None
    assert read_master_snapshot(path, as_of=date(2026, 5, 26)) is None


def test_mean_population_relative_strength_and_snapshot_median_remain_distinct(tmp_path):
    path = tmp_path / "market.sqlite"
    returns = {"AAAA": 0.0, "BBBB": 0.0, "CCCC": 0.9, "DDDD": 0.1}
    sectors = {"AAAA": "機械", "BBBB": "機械", "CCCC": "機械", "DDDD": "小売業"}
    for ticker, ret in returns.items():
        bars(path, ticker, ret)
    master(path, "2026-05-28", sectors)
    mean_context = sector_relative_strength(returns, sectors)
    snapshot = build_market_snapshot(sqlite_path=path, asof_date=ASOF, history_weeks=1)
    row = next(item for item in snapshot["sectors"] if item["sector_33"] == "機械")
    assert mean_context.mean_returns["機械"] == pytest.approx(0.3)
    assert mean_context.relative_returns["機械"] == pytest.approx(0.05)
    assert mean_context.percentiles == {"機械": 1.0, "小売業": 0.5}
    assert row["median_return_20d"] == 0.0
    assert row["pct_above_ma20"] == pytest.approx(1 / 3)
    # Own-vs-peers uses the peer median, rather than either whole-sector statistic.
    assert security(path)["relative"]["sector"]["relative_return_20d"] == pytest.approx(-0.45)


def test_sector_strength_ties_have_one_percentile_independent_of_sector_name():
    result = sector_relative_strength(
        {"AAAA": 0.0, "BBBB": 0.0}, {"AAAA": "Z sector", "BBBB": "A sector"}
    )
    assert result.relative_returns == {"Z sector": 0.0, "A sector": 0.0}
    assert result.percentiles == {"Z sector": 0.75, "A sector": 0.75}


def test_sector_percentile_midrank_handles_partial_ties_nulls_and_input_order():
    values = {"low": -1.0, "C": 0.0, "B": 0.0, "high": 1.0, "missing": None}
    expected = {"low": 0.25, "C": 0.625, "B": 0.625, "high": 1.0, "missing": None}
    assert _rank_to_percentiles(values) == expected
    assert _rank_to_percentiles(dict(reversed(list(values.items())))) == expected
    assert _rank_to_percentiles({"only": 0.0}) == {"only": 1.0}
    assert _rank_to_percentiles({"C": None}) == {"C": None}
    assert _rank_to_percentiles({}) == {}


def test_null_close_split_factor_adjusts_past_prices_without_future_events(tmp_path):
    path = tmp_path / "market.sqlite"
    bars(path, "1321", 0.0)
    connection = open_connection(path)
    try:
        days = [
            row[0]
            for row in connection.execute(
                "SELECT traded_at FROM jquants_daily_bars ORDER BY traded_at"
            )
        ]
        event = days[-2]
        connection.execute("UPDATE jquants_daily_bars SET close=200 WHERE traded_at < ?", (event,))
        connection.execute(
            "UPDATE jquants_daily_bars SET close=NULL,adjustment_factor=0.5 WHERE traded_at=?",
            (event,),
        )
        connection.commit()
    finally:
        connection.close()
    series = read_price_bars(path, start=ASOF - timedelta(days=90), end=ASOF)["1321"]
    assert [bar.price for bar in series] == [100.0] * 20
    assert (
        compute_benchmark_trend(path, ASOF).benchmark_return_20d is None
    )  # null close isn't a bar
    assert compute_benchmark_trend(path, ASOF).benchmark_gap_from_high == 0
    assert (
        build_market_snapshot(sqlite_path=path, asof_date=ASOF)["points"][-1][
            "benchmark_return_20d"
        ]
        is None
    )
    price = security(path, ticker="1321")["price"]
    assert price["gap_from_52w_high"] == 0
    assert price["bollinger_20bar"] == {
        "status": "zero_variance",
        "window_bars": 20,
        "window_start_date": days[0],
        "window_end_date": ASOF.isoformat(),
        "z_score": None,
        "position": None,
    }
    earlier = read_price_bars(
        path, start=ASOF - timedelta(days=90), end=date.fromisoformat(days[-3])
    )["1321"]
    assert all(bar.price == 200 for bar in earlier)


@pytest.mark.parametrize(
    ("closes", "status", "z_score", "position"),
    [
        ([], "insufficient_history", None, None),
        ([100] * 19, "insufficient_history", None, None),
        ([100] * 20, "zero_variance", None, None),
        ([100] * 19 + [120], "available", 19**0.5, "above_upper"),
        ([100] * 19 + [80], "available", -(19**0.5), "below_lower"),
        ([100] * 16 + [110] * 4, "available", 2, "within_bands"),
        ([100] * 16 + [90] * 4, "available", -2, "within_bands"),
        ([100] * 19 + [0], "invalid_price", None, None),
        ([100] * 19 + [-1], "invalid_price", None, None),
        ([float("inf")] + [100] * 19, "invalid_price", None, None),
        ([float("nan")] + [100] * 19, "invalid_price", None, None),
    ],
)
def test_bollinger_observed_window_arithmetic(closes, status, z_score, position):
    observed = [
        PriceBar(ASOF - timedelta(days=3 * (len(closes) - i - 1)), value, None)
        for i, value in enumerate(closes)
    ]
    result = _bollinger_20bar(observed)
    assert result["status"] == status
    assert result["position"] == position
    assert result["z_score"] == (pytest.approx(z_score) if z_score is not None else None)
    assert result["window_bars"] == len(closes)
    assert result["window_start_date"] == (observed[0].traded_at.isoformat() if observed else None)
    assert result["window_end_date"] == (ASOF.isoformat() if observed else None)


def test_bollinger_uses_last_twenty_observed_bars_and_preserves_stale_dates(tmp_path):
    path = tmp_path / "market.sqlite"
    insert_daily_bars_from_closes(path, "AAAA", [1000] + [100] * 19 + [120], end_date=ASOF)
    original = security(path)["price"]["bollinger_20bar"]
    assert original["z_score"] == pytest.approx(19**0.5)
    assert original["window_bars"] == 20
    assert original["window_start_date"] == (ASOF - timedelta(days=19)).isoformat()
    assert security(path, as_of=ASOF + timedelta(days=2))["price"]["bollinger_20bar"] == original
    assert security(path, as_of=ASOF + timedelta(days=30))["price"]["bollinger_20bar"] == original
    connection = open_connection(path)
    try:
        connection.execute(
            "INSERT INTO jquants_daily_bars (ticker, traded_at, close, adjustment_factor) "
            "VALUES ('AAAA', ?, 9999, 0.5)",
            ((ASOF + timedelta(days=31)).isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()
    price = security(path)["price"]
    assert price["bollinger_20bar"] == original
    assert price["close"] == 120


def test_assessment_cutoff_and_domain_import_do_not_reach_research_or_writers(tmp_path):
    path = tmp_path / "app.sqlite"
    for day, prior in (("2026-07-19", None), ("2026-07-20", "macro-context-2026-07-19-past")):
        identifier = f"macro-context-{day}-" + ("past" if prior is None else "future")
        seed_history(
            path,
            (
                StoredAssessment.from_payload(
                    historical_context_payload(
                        context_id=identifier,
                        as_of=day,
                        published_at=f"{day}T12:00:00+09:00",
                    )
                )
            ).document,
            supersedes=prior,
        )
    assert (
        latest_assessment_payload(
            path, as_of=date(2026, 7, 19), known_at=datetime(2026, 7, 21, tzinfo=UTC)
        )["context_id"]
        == "macro-context-2026-07-19-past"
    )
    assert read_context(path, context_id="macro-context-2026-07-20-future")["as_of"] == "2026-07-20"
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from baibai_engine.market_context.reading.query import reading_snapshot
from baibai_engine.market_context.security import build_security_market_context
assert not any(name.startswith(("baibai_engine.research", "baibai_engine.read_api", "baibai_engine.screening", "baibai_engine.position", "baibai_engine.collection.economic.providers")) for name in sys.modules)
assert "baibai_engine.storage.application.write" not in sys.modules
assert "baibai_engine.research.market_context.service" not in sys.modules
""",
        ],
        check=True,
    )


def test_domain_reads_have_no_network_or_writes_and_missing_stores_stay_absent(
    tmp_path, monkeypatch
):
    market_path, indicators_path, app_path = (
        tmp_path / name for name in ("market.sqlite", "indicators.sqlite", "app.sqlite")
    )
    bars(market_path, "AAAA", 0.1)
    master(market_path, "2026-05-28", {"AAAA": "機械"})
    connection = initialize_database(indicators_path)
    connection.commit()
    connection.close()
    seed_history(
        app_path,
        (StoredAssessment.from_payload(historical_context_payload())).document,
        supersedes=None,
    )
    paths = (market_path, indicators_path, app_path)
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    original = sqlite3.connect

    def connect(database, *args, **kwargs):
        assert "mode=ro" in database
        connection = original(database, *args, **kwargs)

        def authorize(action, _a, _b, _db, _trigger):
            assert action not in {
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_CREATE_TABLE,
                sqlite3.SQLITE_DROP_TABLE,
            }
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    def forbidden(*_args, **_kwargs):
        pytest.fail("Market Context read attempted network I/O")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(socket, "socket", forbidden)
    assert security(market_path)["price"] is not None
    assert build_market_snapshot(sqlite_path=market_path, asof_date=ASOF)["sectors"]
    assert reading_snapshot(indicators_path, asof=ASOF)["asof"] == ASOF.isoformat()
    assert latest_assessment_payload(
        app_path, as_of=date(2026, 7, 19), known_at=datetime(2026, 7, 21, tzinfo=UTC)
    )["context_id"]
    assert before == [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    missing = tmp_path / "absent.sqlite"
    assert security(missing)["price"] is None
    assert reading_snapshot(missing, asof=ASOF) is None
    assert (
        latest_assessment_payload(missing, as_of=ASOF, known_at=datetime(2026, 7, 21, tzinfo=UTC))
        is None
    )
    assert not missing.exists()


def test_security_price_basis_includes_rights_price_adjustments(tmp_path):
    path = tmp_path / "market.sqlite"
    bars(path, "AAAA", 0.0)
    connection = open_connection(path)
    try:
        connection.execute(
            "UPDATE jquants_daily_bars SET close=80,adjustment_factor=0.8,ex_right_type='3' "
            "WHERE ticker='AAAA' AND traded_at=?",
            (ASOF.isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()
    price = security(path)["price"]
    assert price["price_basis"] == "price_adjusted_close"
    assert price["close"] == 80
    assert price["return_20d"] == pytest.approx(0.0)


@pytest.mark.parametrize("reader", ["prices", "master"])
def test_market_context_readers_reject_obsolete_market_schema(tmp_path, reader):
    from baibai_engine.storage.observations.schema import (
        SQLITE_SCHEMA_VERSION,
        SQLiteSchemaError,
    )

    path = tmp_path / "market.sqlite"
    bars(path, "AAAA", 0.1)
    master(path, "2026-05-28", {"AAAA": "機械"})
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")
        connection.commit()
    finally:
        connection.close()
    read = (
        partial(read_price_bars, path, start=ASOF - timedelta(days=90), end=ASOF)
        if reader == "prices"
        else partial(read_master_snapshot, path, as_of=ASOF)
    )
    with pytest.raises(SQLiteSchemaError, match="obsolete market SQLite schema"):
        read()
