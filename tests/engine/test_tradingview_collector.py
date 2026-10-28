from __future__ import annotations

import asyncio
import copy
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from mcp.types import CallToolResult
from tests.helpers.master import insert_master_rows

from baibai_jobs.acquisition.tradingview.collector import (
    SourceDataError,
    collect,
    preflight_snapshot,
    universe,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
    FetchError,
    normalize_batch,
)
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.storage.observations.schema import open_connection

DAY = date(2026, 9, 18)
NOW = datetime(2026, 9, 18, 7, tzinfo=UTC)

# Provider-native keys are fixed independently of the production field mapping.
PROVIDER_ROW = {
    "AnalystRating": None,
    "recommendation_total": None,
    "price_target_average": None,
    "price_target_high": None,
    "price_target_low": None,
    "earnings_per_share_forecast_next_fq": None,
    "earnings_per_share_forecast_next_fy": -5,
    "revenue_forecast_next_fq": None,
    "revenue_forecast_next_fy": None,
    "price_earnings_fwd": None,
    "price_sales_fwd": None,
    "enterprise_value_ebitda_fwd": None,
    "ebitda_estimate_fy": None,
    "free_cash_flow_estimate_fy": None,
    "capital_expenditures_estimate_fy": None,
    "total_debt_estimate_fy": None,
    "close": 100,
    "currency": "JPY",
}


def _sdk(body: dict) -> CallToolResult:
    return CallToolResult(isError=False, structuredContent=body, content=[])


def store(path: Path, count: int = 51) -> Path:
    with closing(open_connection(path)) as conn, conn:
        conn.execute("INSERT INTO jquants_market_calendar VALUES (?,1)", (DAY.isoformat(),))
        insert_master_rows(
            conn,
            [
                (DAY.isoformat(), str(1000 + i), "IPO", "プライム", "機械", "011")
                for i in range(count)
            ],
        )
    return path


def payload(symbols: list[str], *, missing: list[str] | None = None) -> dict:
    missing = missing or []
    return {
        "success": True,
        "data": {s: dict(PROVIDER_ROW) for s in symbols if s not in missing},
        "count": len(symbols) - len(missing),
        "missing": [{"symbol": s, "reason": "Quote data not found"} for s in missing],
        "missing_count": len(missing),
    }


def stored(path: Path) -> list:
    with closing(sqlite3.connect(path)) as conn, conn:
        return conn.execute(
            "SELECT * FROM tradingview_forecast_snapshots ORDER BY ticker"
        ).fetchall()


def test_full_universe_batches_and_same_day_immutability(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite")
    calls = []
    pauses = []

    async def fetch(symbols, columns):
        calls.append(symbols)
        assert len(columns) == 18
        assert set(columns) == PROVIDER_ROW.keys()
        response = payload(symbols, missing=symbols[1:2] if len(symbols) == 50 else [])
        for row in response["data"].values():
            row.update(
                AnalystRating="Buy",
                recommendation_total=7,
                price_target_average=170,
                price_target_high=220,
                price_target_low=130,
                earnings_per_share_forecast_next_fq=-1.25,
                revenue_forecast_next_fq=200_000,
                revenue_forecast_next_fy=900_000,
                price_earnings_fwd=13.5,
                price_sales_fwd=2.5,
                enterprise_value_ebitda_fwd=7.5,
                ebitda_estimate_fy=300_000,
                free_cash_flow_estimate_fy=-40_000,
                capital_expenditures_estimate_fy=-50_000,
                total_debt_estimate_fy=600_000,
            )
        return _sdk(response)

    async def sleep(interval):
        pauses.append(interval)

    result = asyncio.run(collect(path, DAY, fetch, clock=lambda: NOW, sleep=sleep))
    assert [len(c) for c in calls] == [50, 1]
    assert pauses == [15]
    assert result["rows"] == 51
    assert result["requests_attempted"] == 2
    assert result["symbols_requested"] == 51
    assert result["unresolved"] == 1
    original = stored(path)
    result = asyncio.run(collect(path, DAY, fetch, clock=lambda: NOW))
    assert result["status"] == "already_saved"
    assert len(calls) == 2
    assert stored(path) == original
    state = preflight_snapshot(path, DAY)
    assert {
        key: state[key]
        for key in (
            "master_asof",
            "is_business_day",
            "expected_universe",
            "remaining",
            "status",
            "snapshot_date",
            "rows",
        )
    } == {
        "master_asof": DAY.isoformat(),
        "is_business_day": True,
        "expected_universe": 51,
        "remaining": 0,
        "status": "already_saved",
        "snapshot_date": DAY.isoformat(),
        "rows": 51,
    }
    assert state["requests_attempted"] == 0
    assert state["unresolved"] == 1
    assert state["first_fetched_at_utc"] == NOW.isoformat()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        saved = conn.execute(
            "SELECT * FROM tradingview_forecast_snapshots WHERE ticker='1000'"
        ).fetchone()
        assert saved is not None
        assert dict(saved) == {
            "snapshot_date": DAY.isoformat(),
            "ticker": "1000",
            "provider_symbol": "TSE:1000",
            "fetched_at_utc": NOW.isoformat(),
            "fetch_status": "ok",
            "quote_at_utc": None,
            "estimate_currency": None,
            "estimate_unit": None,
            "provider_updated_at_utc": None,
            "target_period_label": None,
            "target_period_end": None,
            "analyst_rating": "Buy",
            "recommendation_total": 7,
            "price_target_average": 170,
            "price_target_high": 220,
            "price_target_low": 130,
            "eps_forecast_next_fq": -1.25,
            "eps_forecast_next_fy": -5,
            "revenue_forecast_next_fq": 200_000,
            "revenue_forecast_next_fy": 900_000,
            "forward_pe": 13.5,
            "forward_ps": 2.5,
            "forward_ev_ebitda": 7.5,
            "ebitda_estimate_fy": 300_000,
            "free_cash_flow_estimate_fy": -40_000,
            "capex_estimate_fy": -50_000,
            "total_debt_estimate_fy": 600_000,
            "quote_close": 100,
            "quote_currency": "JPY",
        }
        missing = conn.execute(
            "SELECT fetch_status, recommendation_total FROM tradingview_forecast_snapshots "
            "WHERE ticker='1001'"
        ).fetchone()
        assert missing is not None
        assert dict(missing) == {"fetch_status": "unresolved", "recommendation_total": None}


def test_preflight_is_read_only_and_resumes_partial_rows(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(SourceDataError):
        preflight_snapshot(missing, DAY)
    assert not missing.exists()
    path = store(tmp_path / "market.sqlite", 2)
    assert preflight_snapshot(path, DAY)["needs_master"] is False
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "DELETE FROM jquants_master_snapshots WHERE snapshot_date=?", (DAY.isoformat(),)
        )
    assert preflight_snapshot(path, DAY)["needs_master"] is True
    with closing(sqlite3.connect(path)) as conn, conn:
        insert_master_rows(
            conn,
            [(DAY.isoformat(), str(1000 + i), "IPO", "プライム", "機械", "011") for i in range(2)],
        )

    async def healthy(symbols, _columns):
        return _sdk(payload(symbols))

    asyncio.run(collect(path, DAY, healthy, interval=0, clock=lambda: NOW))
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM tradingview_forecast_snapshots WHERE ticker='1001'")
    assert preflight_snapshot(path, DAY)["remaining"] == 1

    calls = []

    async def resume(symbols, _columns):
        calls.append(symbols)
        return _sdk(payload(symbols))

    assert asyncio.run(collect(path, DAY, resume, clock=lambda: NOW))["rows_added"] == 1
    assert calls == [["TSE:1001"]]


@pytest.mark.parametrize("failure", ["429", "malformed", "crash"])
def test_later_failure_preserves_committed_batch(tmp_path: Path, failure: str) -> None:
    path = store(tmp_path / "market.sqlite")
    calls = 0

    async def fetch(symbols, _columns):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _sdk(payload(symbols))
        if failure == "malformed":
            return _sdk({"success": True})
        if failure == "429":
            from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
                ProviderHTTPError,
            )

            raise ProviderHTTPError(429)
        raise RuntimeError(failure)

    if failure == "crash":
        with pytest.raises(RuntimeError):
            asyncio.run(collect(path, DAY, fetch, interval=0, clock=lambda: NOW))
    else:
        result = asyncio.run(collect(path, DAY, fetch, interval=0, clock=lambda: NOW))
        assert result["status"] == "partial"
        assert result["rows_added"] == 50
        assert result["remaining"] == 1
        assert (
            result["stop_reason"]
            == {
                "429": "provider_rate_limit",
                "malformed": "provider_response",
            }[failure]
        )
    original = stored(path)
    assert len(original) == 50

    # A same-day whole-run retry can still succeed.
    async def healthy(symbols, _columns):
        return _sdk(payload(symbols))

    result = asyncio.run(collect(path, DAY, healthy, interval=0, clock=lambda: NOW))
    assert result["rows"] == 51
    assert result["rows_added"] == 1
    assert stored(path)[:50] == original


def test_bad_middle_batch_skips_then_same_day_resumes_only_gap(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 3)
    calls = []
    pauses = []

    async def fetch(symbols, _columns):
        calls.append(symbols)
        return _sdk({"success": True} if symbols == ["TSE:1001"] else payload(symbols))

    async def sleep(seconds):
        pauses.append(seconds)

    result = asyncio.run(
        collect(path, DAY, fetch, batch_size=1, interval=2, clock=lambda: NOW, sleep=sleep)
    )
    assert result["status"] == "partial"
    assert result["rows_added"] == 2
    assert result["remaining"] == 1
    assert calls == [["TSE:1000"], ["TSE:1001"], ["TSE:1002"]]
    assert pauses == [2, 2]
    original = stored(path)

    async def resume(symbols, _columns):
        calls.append(symbols)
        return _sdk(payload(symbols))

    result = asyncio.run(collect(path, DAY, resume, batch_size=1, clock=lambda: NOW))
    assert result["status"] == "saved"
    assert result["rows_added"] == 1
    assert calls[-1] == ["TSE:1001"]
    assert [row for row in stored(path) if row[1] != "1001"] == original


def test_three_bad_batches_stop_and_zero_new_rows_fail(tmp_path: Path) -> None:
    from baibai_jobs.acquisition.tradingview.collector import CollectionFailure

    path = store(tmp_path / "market.sqlite", 5)
    calls = []

    async def bad(symbols, _columns):
        calls.append(symbols)
        return _sdk({"success": True})

    with pytest.raises(CollectionFailure):
        asyncio.run(collect(path, DAY, bad, batch_size=1, interval=0, clock=lambda: NOW))
    assert len(calls) == 3
    assert stored(path) == []


def test_prior_partial_does_not_make_zero_new_rows_successful(tmp_path: Path) -> None:
    from baibai_jobs.acquisition.tradingview.collector import CollectionFailure
    from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
        ProviderHTTPError,
    )

    path = store(tmp_path / "market.sqlite", 2)

    async def first(symbols, _columns):
        if symbols == ["TSE:1001"]:
            raise ProviderHTTPError(429)
        return _sdk(payload(symbols))

    result = asyncio.run(collect(path, DAY, first, batch_size=1, interval=0, clock=lambda: NOW))
    assert result["rows_added"] == 1

    async def blocked(symbols, _columns):
        assert symbols == ["TSE:1001"]
        raise ProviderHTTPError(429)

    with pytest.raises(CollectionFailure):
        asyncio.run(collect(path, DAY, blocked, batch_size=1, interval=0, clock=lambda: NOW))
    assert len(stored(path)) == 1


def test_unknown_stored_ticker_blocks_preflight_and_provider(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 2)

    async def healthy(symbols, _columns):
        return _sdk(payload(symbols))

    asyncio.run(collect(path, DAY, healthy, clock=lambda: NOW))
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE tradingview_forecast_snapshots SET ticker='9999' WHERE ticker='1001'")
    with pytest.raises(SourceDataError, match="outside"):
        preflight_snapshot(path, DAY)

    async def forbidden(_symbols, _columns):
        pytest.fail("inconsistent source must not reach provider")

    with pytest.raises(SourceDataError, match="outside"):
        asyncio.run(collect(path, DAY, forbidden, clock=lambda: NOW))


def test_soft_budget_keeps_first_batch_without_next_request(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 2)
    timer = Timer()
    calls = []

    async def fetch(symbols, _columns):
        calls.append(symbols)
        timer.value = 25 * 60
        return _sdk(payload(symbols))

    async def no_sleep(_seconds):
        pass

    result = asyncio.run(
        collect(
            path,
            DAY,
            fetch,
            batch_size=1,
            interval=0,
            clock=lambda: NOW,
            timer=timer,
            sleep=no_sleep,
        )
    )
    assert (result["status"], result["rows_added"], result["stop_reason"]) == (
        "partial",
        1,
        "soft_budget",
    )
    assert calls == [["TSE:1000"]]


def test_soft_budget_stops_before_interval_that_exceeds_remaining_time(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 2)
    timer = Timer()
    calls = []
    pauses = []

    async def fetch(symbols, _columns):
        calls.append(symbols)
        timer.value = 20 * 60
        return _sdk(payload(symbols))

    async def sleep(seconds):
        pauses.append(seconds)
        timer.value += seconds

    result = asyncio.run(
        collect(
            path,
            DAY,
            fetch,
            batch_size=1,
            interval=10 * 60,
            clock=lambda: NOW,
            timer=timer,
            sleep=sleep,
        )
    )
    assert (result["status"], result["rows_added"], result["stop_reason"]) == (
        "partial",
        1,
        "soft_budget",
    )
    assert calls == [["TSE:1000"]]
    assert pauses == []
    assert len(stored(path)) == 1


def test_good_batch_resets_consecutive_bad_limit(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 6)
    calls = []

    async def fetch(symbols, _columns):
        calls.append(symbols)
        if symbols[0] in {"TSE:1000", "TSE:1001", "TSE:1003", "TSE:1004"}:
            return _sdk({"success": True})
        return _sdk(payload(symbols))

    result = asyncio.run(collect(path, DAY, fetch, batch_size=1, interval=0, clock=lambda: NOW))
    assert result["status"] == "partial"
    assert result["rows_added"] == 2
    assert len(calls) == 6


@pytest.mark.parametrize(
    "observed", [NOW - timedelta(days=1), NOW + timedelta(days=1), NOW.replace(hour=5)]
)
def test_no_backfill_or_preclose_collection(tmp_path: Path, observed: datetime) -> None:
    path = store(tmp_path / "market.sqlite", 1)

    async def forbidden(symbols, _columns):
        pytest.fail("provider must not be called")

    with pytest.raises(FetchError, match="backfill"):
        asyncio.run(collect(path, DAY, forbidden, clock=lambda: observed))
    assert not stored(path)


def test_midnight_crossing_aborts_the_run(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 1)
    times = iter([NOW, NOW, NOW + timedelta(days=1)])

    async def fetch(symbols, _columns):
        return _sdk(payload(symbols))

    with pytest.raises(FetchError):
        asyncio.run(collect(path, DAY, fetch, clock=lambda: next(times)))
    assert not stored(path)


def test_universe_excludes_secondary_instruments_without_bar_history(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 1)
    conn = open_connection(path)
    with conn:
        insert_master_rows(
            conn,
            [
                (DAY.isoformat(), "2000", "fund", "プライム", "その他", "014"),
                (DAY.isoformat(), "2001", "pro", "TOKYO PRO MARKET", "機械", "011"),
                (DAY.isoformat(), "2002", "Nagoya", "名証", "機械", "011"),
            ],
        )
    assert universe(conn, DAY) == ["TSE:1000"]
    with conn:
        conn.execute(
            "UPDATE jquants_master_snapshots SET record_json=json_set(record_json,'$.ProdCat',NULL) WHERE security_code='10000'"
        )
    with pytest.raises(JQuantsProviderError, match="product category"):
        universe(conn, DAY)
    conn.close()


def test_exact_date_master_and_calendar_are_required(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 1)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM jquants_master_snapshots")

    async def forbidden(symbols, _columns):
        pytest.fail("provider must not be called")

    with pytest.raises(FetchError, match="Exact-date"):
        asyncio.run(collect(path, DAY, forbidden, clock=lambda: NOW))


@pytest.mark.parametrize("forecast", [None, 0, -5])
def test_normal_null_zero_and_negative_forecast_are_not_unresolved(forecast) -> None:
    data = payload(["TSE:1000"])
    data["data"]["TSE:1000"]["earnings_per_share_forecast_next_fy"] = forecast
    result = normalize_batch(data, ["TSE:1000"], NOW)
    assert result[0]["fetch_status"] == "ok"
    assert result[0]["recommendation_total"] is None
    assert result[0]["eps_forecast_next_fy"] == forecast


@pytest.mark.parametrize(
    "case", ["unknown", "overlap", "duplicate_missing", "count", "missing_field", "nan"]
)
def test_response_accounting_refuses_invalid_payload(case: str) -> None:
    symbols = ["TSE:1000", "TSE:1001"]
    p = copy.deepcopy(payload(symbols, missing=[symbols[1]]))
    if case == "unknown":
        p["data"]["NAG:1000"] = p["data"].pop(symbols[0])
    elif case == "overlap":
        p["missing"][0]["symbol"] = symbols[0]
    elif case == "duplicate_missing":
        p["missing"].append(p["missing"][0])
    elif case == "count":
        p["count"] = 99
    elif case == "missing_field":
        p["data"][symbols[0]].pop("close")
    else:
        p["data"][symbols[0]]["close"] = float("nan")
    with pytest.raises(FetchError):
        normalize_batch(p, symbols, NOW)


def test_native_product_category_does_not_depend_on_sector_spelling(tmp_path):
    path = store(tmp_path / "market.sqlite", 1)
    with closing(open_connection(path)) as conn, conn:
        conn.execute(
            "UPDATE jquants_master_snapshots "
            "SET record_json=json_set(record_json,'$.S33Nm','新業種')"
        )
        assert universe(conn, DAY) == ["TSE:1000"]


class Timer:
    value = 0.0

    def __call__(self):
        return self.value


@pytest.mark.parametrize("failure", [None, "429", "malformed", "store", "time_guard"])
def test_progress_and_durations_with_batch_commits(tmp_path, failure):
    from dataclasses import asdict

    from baibai_jobs.acquisition.tradingview.cli import failure_category
    from baibai_jobs.acquisition.tradingview.collector import CollectionFailure
    from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
        ProviderHTTPError,
    )

    path = store(tmp_path / "market.sqlite")
    if failure == "store":
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "CREATE TRIGGER fail_insert BEFORE INSERT ON tradingview_forecast_snapshots "
                "WHEN NEW.ticker='1050' BEGIN SELECT RAISE(ABORT, 'private-token'); END"
            )
    timer = Timer()
    events = []
    calls = 0
    pauses = []

    async def fetch(symbols, _columns):
        nonlocal calls
        calls += 1
        timer.value += calls * 1.23456
        if calls == 2:
            if failure == "429":
                raise ProviderHTTPError(429)
            if failure == "malformed":
                return _sdk({"success": True})
        return _sdk(payload(symbols))

    async def sleep(interval):
        pauses.append(interval)
        timer.value += interval

    def clock():
        return NOW + timedelta(
            days=1 if failure == "time_guard" and calls == 2 else 0, seconds=timer.value
        )

    async def run():
        return await collect(
            path, DAY, fetch, clock=clock, timer=timer, sleep=sleep, progress=events.append
        )

    if failure == "store":
        with pytest.raises(CollectionFailure) as caught:
            asyncio.run(run())
        error = caught.value
        assert failure_category(error) == "storage"
        assert error.progress == events[-1]
        assert len(stored(path)) == 50
    else:
        result = asyncio.run(run())
        assert result["rows"] == (51 if failure is None else 50)
        assert result["rows_added"] == result["rows"]
        assert result["unresolved"] == 0
        assert result["status"] == ("saved" if failure is None else "partial")
        assert result["chunks_total"] == 2
        assert result["columns_count"] == 18
        assert result["interval_seconds"] == 15
        assert result["first_fetched_at_utc"] == (NOW + timedelta(seconds=1.23456)).isoformat()
        assert events[-1].phase == "complete"
        assert [e.phase for e in events].count("store") == (2 if failure is None else 1)
        if failure == "malformed":
            assert result["stop_reason"] == "provider_response"
    assert calls == 2
    assert pauses == [15]
    assert events[-1].provider_elapsed_seconds == 3.704
    assert events[-1].max_chunk_elapsed_seconds == 2.469
    assert events[-1].elapsed_seconds == 18.704
    assert events[-1].response_bytes > 0
    assert events[-1].expected_universe == 51
    assert events[-1].chunks_total == 2
    assert "TSE:" not in repr([asdict(e) for e in events])


@pytest.mark.parametrize(
    ("provider_field", "field", "value", "reason"),
    [
        ("close", "quote_close", float("nan"), "invalid_numeric"),
        ("recommendation_total", "recommendation_total", -1, "invalid_analyst_count"),
        ("currency", "quote_currency", 42, "invalid_text_type"),
    ],
)
def test_payload_diagnostics_use_canonical_field(provider_field, field, value, reason):
    from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
        ProviderPayloadError,
    )

    data = payload(["TSE:1000"])
    data["data"]["TSE:1000"][provider_field] = value
    with pytest.raises(ProviderPayloadError) as caught:
        normalize_batch(data, ["TSE:1000"], NOW)
    assert (caught.value.reason, caught.value.field) == (reason, field)


def test_missing_fields_uses_first_requested_canonical_field():
    from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
        ProviderPayloadError,
    )

    data = payload(["TSE:1000"])
    del data["data"]["TSE:1000"]["earnings_per_share_forecast_next_fy"]
    del data["data"]["TSE:1000"]["close"]
    with pytest.raises(ProviderPayloadError) as caught:
        normalize_batch(data, ["TSE:1000"], NOW)
    assert caught.value.field == "eps_forecast_next_fy"


@pytest.mark.parametrize("raw", [None, [], "private-body", 42])
def test_non_dict_row_does_not_blame_a_field(raw):
    from baibai_jobs.acquisition.tradingview.cli import failure_line
    from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
        ProviderPayloadError,
    )

    data = payload(["TSE:1000"])
    data["data"]["TSE:1000"] = raw
    with pytest.raises(ProviderPayloadError) as caught:
        normalize_batch(data, ["TSE:1000"], NOW)
    assert caught.value.reason == "row_missing_requested_fields"
    assert caught.value.field is None
    assert "validation_field=-" in failure_line(caught.value)
    assert "private-body" not in failure_line(caught.value)


def test_partial_http_failure_keeps_status_and_wait_instruction(tmp_path):
    import httpx2

    path = store(tmp_path / "market.sqlite", 2)
    calls = []

    async def fetch(symbols, _columns):
        calls.append(symbols)
        if len(calls) == 2:
            response = httpx2.Response(
                429,
                headers={"Retry-After": "Sun, 11 Oct 2026 18:00:00 GMT"},
                request=httpx2.Request("GET", "https://example.com"),
            )
            raise httpx2.HTTPStatusError("private", request=response.request, response=response)
        return _sdk(payload(symbols))

    result = asyncio.run(collect(path, DAY, fetch, batch_size=1, interval=0, clock=lambda: NOW))
    assert result["status"] == "partial"
    assert result["rows_added"] == 1
    assert result["requests_attempted"] == 2
    assert result["symbols_requested"] == 2
    assert "mcp_http_status=429" in result["last_failure"]
    assert "retry_not_before_utc=2026-10-11T18:00:00+00:00" in result["last_failure"]
    assert "private" not in str(result)


@pytest.mark.parametrize("batch_size", [1, 2])
def test_explicit_missing_receipt_does_not_depend_on_batch_composition(batch_size):
    symbols = ["TSE:1000", "TSE:1001"]
    rows = []
    for offset in range(0, len(symbols), batch_size):
        chunk = symbols[offset : offset + batch_size]
        response = payload(chunk, missing=[s for s in chunk if s == symbols[1]])
        rows.extend(normalize_batch(response, chunk, NOW))
    assert rows == normalize_batch(payload(symbols, missing=[symbols[1]]), symbols, NOW)
    assert rows[1] == {
        "ticker": "1001",
        "provider_symbol": "TSE:1001",
        "fetched_at_utc": NOW.isoformat(),
        "fetch_status": "unresolved",
        "analyst_rating": None,
        "recommendation_total": None,
        "price_target_average": None,
        "price_target_high": None,
        "price_target_low": None,
        "eps_forecast_next_fq": None,
        "eps_forecast_next_fy": None,
        "revenue_forecast_next_fq": None,
        "revenue_forecast_next_fy": None,
        "forward_pe": None,
        "forward_ps": None,
        "forward_ev_ebitda": None,
        "ebitda_estimate_fy": None,
        "free_cash_flow_estimate_fy": None,
        "capex_estimate_fy": None,
        "total_debt_estimate_fy": None,
        "quote_close": None,
        "quote_currency": None,
    }


def test_final_unavailable_symbol_is_recorded_without_false_forecast_coverage(tmp_path):
    path = store(tmp_path / "market.sqlite", 51)
    calls = []

    async def fetch(symbols, _columns):
        calls.append(symbols)
        return _sdk(payload(symbols, missing=[s for s in symbols if s == "TSE:1050"]))

    async def no_sleep(_seconds):
        pass

    result = asyncio.run(collect(path, DAY, fetch, clock=lambda: NOW, sleep=no_sleep))
    assert (result["status"], result["rows"], result["remaining"], result["unresolved"]) == (
        "saved",
        51,
        0,
        1,
    )
    assert [len(call) for call in calls] == [50, 1]
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT fetch_status, quote_close, recommendation_total "
            "FROM tradingview_forecast_snapshots WHERE ticker='1050'"
        ).fetchone() == ("unresolved", None, None)
    again = asyncio.run(collect(path, DAY, fetch, clock=lambda: NOW))
    assert again["status"] == "already_saved"
    assert len(calls) == 2


def test_successful_all_missing_day_is_accounted_but_not_normal_null_data(tmp_path):
    path = store(tmp_path / "market.sqlite", 2)

    async def fetch(symbols, _columns):
        return _sdk(payload(symbols, missing=symbols))

    result = asyncio.run(collect(path, DAY, fetch, clock=lambda: NOW))
    assert (result["status"], result["rows"], result["unresolved"]) == ("saved", 2, 2)
    assert all(count == 0 for count in result["normal_nulls"].values())
    state = preflight_snapshot(path, DAY)
    assert (state["status"], state["unresolved"]) == ("already_saved", 2)
