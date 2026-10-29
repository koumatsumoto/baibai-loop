"""Live receipt capture and explicit offline replay share one observation meaning."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.types import CallToolResult, TextContent
from tests.engine.test_tradingview_collector import DAY, NOW, _sdk, payload, store, stored

import baibai_jobs.acquisition.tradingview.cli as cli
from baibai_engine.data_collection.providers.tradingview.contract import BATCH_TOOL
from baibai_engine.data_collection.providers.tradingview.receipts import (
    read_tool_results,
    retain_tool_result,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
    COLUMNS,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.tradingview.collector import (
    CollectionFailure,
    SourceDataError,
    collect,
)
from baibai_jobs.maintenance.tradingview_rebuild import (
    RebuildError,
    rebuild_from_receipts,
)
from baibai_jobs.observability.tradingview import (
    failure_category,
)


def _ids(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT receipt_id FROM tradingview_source_receipts ORDER BY rowid"
            )
        ]


def _copy_receipts_only(source: Path, target: Path, receipt_ids: list[str]) -> None:
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(target)) as clean:
        with clean:
            for table, order in (
                ("jquants_market_calendar", "day"),
                ("jquants_master_snapshots", "security_code"),
            ):
                assert original.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall() == (  # nosec B608
                    clean.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()  # nosec B608
                )
            for receipt_id in receipt_ids:
                row = original.execute(
                    "SELECT * FROM tradingview_source_receipts WHERE receipt_id=?", (receipt_id,)
                ).fetchone()
                assert row is not None
                clean.execute(
                    "INSERT INTO tradingview_source_receipts VALUES (?,?,?,?,?,?,?,?)", row
                )
        assert clean.execute("SELECT COUNT(*) FROM tradingview_forecast_snapshots").fetchone() == (
            0,
        )


def _retain(
    path: Path,
    result: CallToolResult,
    *,
    day: date = DAY,
    master: date = DAY,
    columns: tuple[str, ...] = tuple(COLUMNS),
    received=NOW,
    symbols: tuple[str, ...] = ("TSE:1000",),
) -> str:
    with closing(open_connection(path)) as conn:
        return retain_tool_result(
            conn,
            snapshot_date=day,
            master_asof=master,
            tool_name=BATCH_TOOL,
            received_at_utc=received,
            requested_symbols=symbols,
            requested_columns=columns,
            result=result,
        )


def test_live_partial_retry_and_offline_rebuild_match_exact_rows(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 3)
    requested: list[tuple[list[str], list[str]]] = []

    async def fetch(symbols: list[str], columns: list[str]) -> CallToolResult:
        requested.append((symbols, columns))
        if symbols == ["TSE:1001"]:
            return _sdk({"success": True})
        return _sdk(payload(symbols, missing=symbols if symbols == ["TSE:1002"] else []))

    first = asyncio.run(collect(path, DAY, fetch, batch_size=1, interval=0, clock=lambda: NOW))
    assert (first["status"], first["rows"], first["unresolved"], first["remaining"]) == (
        "partial",
        2,
        1,
        1,
    )
    assert [symbols for symbols, _ in requested] == [["TSE:1000"], ["TSE:1001"], ["TSE:1002"]]
    assert all(columns == COLUMNS for _, columns in requested)
    first_ids = _ids(path)
    assert len(first_ids) == 3
    before = stored(path)
    clean_partial = store(tmp_path / "clean-partial.sqlite", 3)
    _copy_receipts_only(path, clean_partial, first_ids)
    rebuilt_clean = rebuild_from_receipts(clean_partial, DAY, first_ids)
    assert stored(clean_partial) == before
    assert (rebuilt_clean["rows"], rebuilt_clean["unresolved"], rebuilt_clean["remaining"]) == (
        2,
        1,
        1,
    )
    rebuilt = rebuild_from_receipts(path, DAY, first_ids)
    assert stored(path) == before
    assert (
        rebuilt["rows"],
        rebuilt["unresolved"],
        rebuilt["expected_universe"],
        rebuilt["remaining"],
    ) == (2, 1, 3, 1)
    assert rebuilt["rejected_receipts"] == [
        {"receipt_id": first_ids[1], "reason": "provider_response"}
    ]

    async def retry(symbols: list[str], columns: list[str]) -> CallToolResult:
        assert symbols == ["TSE:1001"]
        assert columns == COLUMNS
        return _sdk(payload(symbols))

    second = asyncio.run(collect(path, DAY, retry, batch_size=1, interval=0, clock=lambda: NOW))
    assert (second["status"], second["rows_added"], second["remaining"]) == ("saved", 1, 0)
    incremental = stored(path)
    final_ids = _ids(path)
    assert len(final_ids) == 4
    clean_final = store(tmp_path / "clean-final.sqlite", 3)
    _copy_receipts_only(path, clean_final, final_ids)
    final_clean = rebuild_from_receipts(clean_final, DAY, final_ids)
    assert stored(clean_final) == incremental
    assert (final_clean["rows"], final_clean["unresolved"], final_clean["remaining"]) == (3, 1, 0)
    final = rebuild_from_receipts(path, DAY, final_ids)
    assert stored(path) == incremental
    assert (final["rows"], final["unresolved"], final["remaining"]) == (3, 1, 0)


def test_all_missing_normal_receipt_rebuilds_the_live_unresolved_row(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 1)

    async def fetch(symbols: list[str], _columns: list[str]) -> CallToolResult:
        return _sdk(payload(symbols, missing=symbols))

    live = asyncio.run(collect(path, DAY, fetch, clock=lambda: NOW))
    assert (live["status"], live["rows"], live["unresolved"], live["remaining"]) == (
        "saved",
        1,
        1,
        0,
    )
    receipt_ids = _ids(path)
    clean = store(tmp_path / "clean.sqlite", 1)
    _copy_receipts_only(path, clean, receipt_ids)
    rebuilt = rebuild_from_receipts(clean, DAY, receipt_ids)
    assert (rebuilt["rows"], rebuilt["unresolved"], rebuilt["remaining"]) == (1, 1, 0)
    assert stored(clean) == stored(path)


@pytest.mark.parametrize("kind", ["sdk_error", "text_error", "malformed_text"])
def test_received_error_is_saved_before_rejection(tmp_path: Path, kind: str) -> None:
    path = store(tmp_path / "market.sqlite", 1)
    if kind == "sdk_error":
        response = CallToolResult(isError=True, content=[TextContent(type="text", text="HTTP 429")])
        expected = "provider_rate_limit"
    elif kind == "text_error":
        response = CallToolResult(
            isError=False,
            content=[TextContent(type="text", text='{"success":false,"error":"HTTP 429"}')],
        )
        expected = "provider_rate_limit"
    else:
        response = CallToolResult(isError=False, content=[TextContent(type="text", text="{bad")])
        expected = "provider_response"

    async def fetch(_symbols: list[str], _columns: list[str]) -> CallToolResult:
        return response

    with pytest.raises(CollectionFailure) as caught:
        asyncio.run(collect(path, DAY, fetch, clock=lambda: NOW))
    assert failure_category(caught.value) == expected
    assert stored(path) == []
    ids = _ids(path)
    assert len(ids) == 1
    with closing(open_connection(path)) as conn:
        saved = json.loads(read_tool_results(conn, ids)[0].result_json)
    assert saved["isError"] == response.is_error
    with pytest.raises(RebuildError) as replay:
        rebuild_from_receipts(path, DAY, ids)
    assert replay.value.rejected == ({"receipt_id": ids[0], "reason": expected},)
    assert stored(path) == []


def test_raw_store_failure_prevents_l2_and_late_response_keeps_raw(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 1)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TRIGGER fail_raw BEFORE INSERT ON tradingview_source_receipts "
            "BEGIN SELECT RAISE(ABORT, 'local storage failure'); END"
        )

    async def fetch(symbols: list[str], _columns: list[str]) -> CallToolResult:
        return _sdk(payload(symbols))

    with pytest.raises(CollectionFailure) as caught:
        asyncio.run(collect(path, DAY, fetch, clock=lambda: NOW))
    assert failure_category(caught.value) == "storage"
    assert stored(path) == []
    assert _ids(path) == []
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DROP TRIGGER fail_raw")
    times = iter((NOW, NOW, NOW + timedelta(days=1)))
    with pytest.raises(CollectionFailure) as late:
        asyncio.run(collect(path, DAY, fetch, clock=lambda: next(times)))
    assert failure_category(late.value) == "time_guard"
    assert len(_ids(path)) == 1
    assert stored(path) == []
    with pytest.raises(RebuildError) as replay:
        rebuild_from_receipts(path, DAY, _ids(path))
    assert replay.value.rejected[0]["reason"] == "time_guard"


def test_replay_refuses_overlap_and_wrong_context_without_changing_l2(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 1)
    good = _retain(path, _sdk(payload(["TSE:1000"])))
    rebuild_from_receipts(path, DAY, (good,))
    with closing(sqlite3.connect(path)) as conn, conn:
        row = conn.execute(
            "SELECT * FROM tradingview_forecast_snapshots WHERE snapshot_date=?",
            (DAY.isoformat(),),
        ).fetchone()
        conn.execute(
            f"INSERT INTO tradingview_forecast_snapshots VALUES ({','.join('?' for _ in row)})",  # nosec B608
            ("2026-09-19", *row[1:]),
        )
    baseline = stored(path)
    duplicate = _retain(path, _sdk(payload(["TSE:1000"])))
    with pytest.raises(SourceDataError, match="overlap"):
        rebuild_from_receipts(path, DAY, (good, duplicate))
    for bad_ids in ((good, good),):
        with pytest.raises(SourceDataError, match="unique"):
            rebuild_from_receipts(path, DAY, bad_ids)
    wrong_day = _retain(path, _sdk(payload(["TSE:1000"])), day=DAY + timedelta(days=1))
    wrong_master = _retain(path, _sdk(payload(["TSE:1000"])), master=DAY - timedelta(days=1))
    wrong_columns = _retain(path, _sdk(payload(["TSE:1000"])), columns=("close",))
    outside = _retain(path, _sdk(payload(["TSE:9999"])), symbols=("TSE:9999",))
    for bad_id in (wrong_day, wrong_master, wrong_columns, outside):
        with pytest.raises(SourceDataError):
            rebuild_from_receipts(path, DAY, (good, bad_id))
    assert stored(path) == baseline
    rebuild_from_receipts(path, DAY, (good,))
    assert stored(path) == baseline  # Other dates remain untouched.


def test_rebuild_rolls_back_deletion_if_insert_fails(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 1)
    receipt_id = _retain(path, _sdk(payload(["TSE:1000"])))
    rebuild_from_receipts(path, DAY, (receipt_id,))
    baseline = stored(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TRIGGER fail_rebuild BEFORE INSERT ON tradingview_forecast_snapshots "
            "BEGIN SELECT RAISE(ABORT, 'insertion failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="insertion failure"):
        rebuild_from_receipts(path, DAY, (receipt_id,))
    assert stored(path) == baseline


def test_rebuild_cli_is_offline_and_does_not_create_missing_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "absent.sqlite"
    with patch("socket.socket", side_effect=AssertionError("network")):
        assert (
            cli.main(
                [
                    "rebuild",
                    "--sqlite",
                    str(missing),
                    "--asof",
                    DAY.isoformat(),
                    "--receipt-id",
                    "x",
                ]
            )
            == 1
        )
    assert not missing.exists()
    path = store(tmp_path / "market.sqlite", 1)
    receipt_id = _retain(path, _sdk(payload(["TSE:1000"])))
    with patch("socket.socket", side_effect=AssertionError("network")):
        assert (
            cli.main(
                [
                    "rebuild",
                    "--sqlite",
                    str(path),
                    "--asof",
                    DAY.isoformat(),
                    "--receipt-id",
                    receipt_id,
                ]
            )
            == 0
        )
    result = json.loads(capsys.readouterr().out)
    assert (result["rows"], result["remaining"]) == (1, 0)


@pytest.mark.parametrize("command", ["refresh", "smoke", "authorize", "accept-observations"])
def test_live_or_normalized_acquisition_commands_are_not_operational(command) -> None:
    with (
        patch("socket.socket", side_effect=AssertionError("network")),
        pytest.raises(SystemExit) as error,
    ):
        cli.main([command])
    assert error.value.code == 2
