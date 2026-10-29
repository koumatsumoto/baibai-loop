"""Explicit receipt selections preserve original facts through offline replay."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.types import CallToolResult, TextContent
from tests.helpers.tradingview import DAY, NOW, _sdk, payload, store, stored

import baibai_jobs.maintenance.tradingview_cli as cli
from baibai_engine.data_collection.providers.tradingview.contract import BATCH_TOOL
from baibai_engine.data_collection.providers.tradingview.receipts import (
    read_tool_results,
    retain_tool_result,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.observations import (
    COLUMNS,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.observability.tradingview import SourceDataError
from baibai_jobs.maintenance.tradingview_rebuild import (
    RebuildError,
    rebuild_from_receipts,
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


def test_partial_and_later_selected_receipts_rebuild_exact_rows(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 3)
    first_ids = [
        _retain(path, _sdk(payload(["TSE:1000"]))),
        _retain(path, _sdk({"success": True}), symbols=("TSE:1001",)),
        _retain(path, _sdk(payload(["TSE:1002"], missing=["TSE:1002"])), symbols=("TSE:1002",)),
    ]
    partial = rebuild_from_receipts(path, DAY, first_ids)
    assert (partial["rows"], partial["unresolved"], partial["remaining"]) == (2, 1, 1)
    assert partial["rejected_receipts"] == [
        {"receipt_id": first_ids[1], "reason": "provider_response"}
    ]
    first_rows = stored(path)
    clean = store(tmp_path / "clean.sqlite", 3)
    _copy_receipts_only(path, clean, first_ids)
    rebuild_from_receipts(clean, DAY, first_ids)
    assert stored(clean) == first_rows
    final_ids = [*first_ids, _retain(path, _sdk(payload(["TSE:1001"])), symbols=("TSE:1001",))]
    final = rebuild_from_receipts(path, DAY, final_ids)
    assert (final["rows"], final["unresolved"], final["remaining"]) == (3, 1, 0)
    assert [row for row in stored(path) if row[1] != "1001"] == first_rows
    rebuilt = store(tmp_path / "rebuilt.sqlite", 3)
    _copy_receipts_only(path, rebuilt, final_ids)
    rebuild_from_receipts(rebuilt, DAY, final_ids)
    assert stored(rebuilt) == stored(path)


def test_all_missing_normal_receipt_rebuilds_unresolved_not_null(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 1)
    receipt_id = _retain(path, _sdk(payload(["TSE:1000"], missing=["TSE:1000"])))
    result = rebuild_from_receipts(path, DAY, [receipt_id])
    assert (result["rows"], result["unresolved"], result["remaining"]) == (1, 1, 0)
    assert stored(path)[0][4] == "unresolved"


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

    _retain(path, response)
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


def test_failed_raw_write_and_misdated_receipt_never_create_l2(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 1)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TRIGGER fail_raw BEFORE INSERT ON tradingview_source_receipts "
            "BEGIN SELECT RAISE(ABORT, 'local storage failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        _retain(path, _sdk(payload(["TSE:1000"])))
    assert _ids(path) == []
    assert stored(path) == []
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DROP TRIGGER fail_raw")
    receipt_id = _retain(path, _sdk(payload(["TSE:1000"])), received=NOW + timedelta(days=1))
    with pytest.raises(RebuildError) as replay:
        rebuild_from_receipts(path, DAY, [receipt_id])
    assert replay.value.rejected == ({"receipt_id": receipt_id, "reason": "time_guard"},)
    assert _ids(path) == [receipt_id]
    assert stored(path) == []


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
