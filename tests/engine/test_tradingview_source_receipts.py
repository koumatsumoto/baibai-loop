"""Received SDK tool results survive exact selection and fixed lake publication."""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from mcp.types import CallToolResult
from tests.helpers.l1_release import market_store
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.data_collection.tradingview.receipts import (
    TradingViewReceiptError,
    read_tool_results,
    retain_tool_result,
)
from baibai_engine.storage.lake.datasets import LAKE_DATASETS
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.hydrate import hydrate_market_store
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.reader import resolve_release
from baibai_engine.storage.lake.release import create_release
from baibai_engine.storage.lake.sources import sha256_file
from baibai_engine.storage.lake.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.observations.lake_origin import LakeStoreOrigin, write_lake_store_origin
from baibai_engine.storage.observations.schema import open_connection

DAY = date(2026, 10, 16)
MASTER = date(2026, 10, 15)
RECEIVED = datetime(2026, 10, 16, 7, 30, tzinfo=UTC)
DATASETS = ("jquants.daily_bars", "tradingview.source_receipts")


def _result() -> CallToolResult:
    return CallToolResult.model_validate(
        {
            "_meta": {"source": "fiction", "optional": None},
            "content": [
                {"type": "text", "text": "原文\nそのまま", "_meta": {"segment": 1}},
            ],
            "structuredContent": {
                "unexpected_field": {"blank": None, "ratio": 1.25, "count": 3, "valid": False},
                "rows": ["A", "B"],
            },
            "isError": True,
            "unknownTopLevel": "SDK discards this",
        }
    )


def _retain(conn: sqlite3.Connection, result: CallToolResult | None = None) -> str:
    return retain_tool_result(
        conn,
        snapshot_date=DAY,
        master_asof=MASTER,
        tool_name="mcp-tv-get-symbol-data-batch",
        received_at_utc=RECEIVED,
        requested_symbols=("TSE:2000", "TSE:1000"),
        requested_columns=("close", "earnings_per_share_forecast_next_fy"),
        result=result if result is not None else _result(),
    )


def _rows(path: Path) -> tuple[tuple[object, ...], ...]:
    with closing(sqlite3.connect(path)) as conn:
        return tuple(conn.execute("SELECT * FROM tradingview_source_receipts ORDER BY receipt_id"))


def test_sdk_result_values_and_explicit_selection_are_retained(tmp_path: Path) -> None:
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        first = _retain(conn)
        second = _retain(conn)
        assert first != second  # Equal reception times do not collapse separate calls.
        assert uuid.UUID(hex=first).version == uuid.UUID(hex=second).version == 4
        selected = read_tool_results(conn, (second, first))
        assert [item.receipt_id for item in selected] == [second, first]
        assert all(item.snapshot_date == DAY and item.master_asof == MASTER for item in selected)
        assert all(item.received_at_utc == RECEIVED for item in selected)
        assert selected[0].tool_name == "mcp-tv-get-symbol-data-batch"
        assert selected[0].requested_symbols == ("TSE:2000", "TSE:1000")
        assert selected[0].requested_columns == ("close", "earnings_per_share_forecast_next_fy")
        saved = json.loads(selected[0].result_json)
        assert saved["structuredContent"] == {
            "unexpected_field": {"blank": None, "ratio": 1.25, "count": 3, "valid": False},
            "rows": ["A", "B"],
        }
        assert saved["content"][0]["text"] == "原文\nそのまま"
        assert saved["content"][0]["_meta"] == {"segment": 1}
        assert saved["isError"] is True
        assert saved["_meta"] == {"source": "fiction", "optional": None}
        assert saved["content"][0]["annotations"] is None
        assert "unknownTopLevel" not in saved  # Already discarded by the SDK model.
        with pytest.raises(TradingViewReceiptError, match="not retained"):
            read_tool_results(conn, ("absent",))


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nonfinite_result_is_rejected_before_store(tmp_path: Path, bad: float) -> None:
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        result = CallToolResult.model_validate(
            {"content": [{"type": "text", "text": "x"}], "structuredContent": {"value": bad}}
        )
        with pytest.raises(ValueError, match="Out of range float"):
            _retain(conn, result)
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM tradingview_source_receipts").fetchone() == (0,)


def test_unsupported_python_object_is_not_stringified(tmp_path: Path) -> None:
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        result = CallToolResult.model_validate(
            {"content": [], "structuredContent": {"value": object()}}
        )
        with pytest.raises(TypeError, match="not JSON serializable"):
            _retain(conn, result)
        assert conn.execute("SELECT COUNT(*) FROM tradingview_source_receipts").fetchone() == (0,)


def test_write_rejects_pending_transaction_and_read_preserves_it(tmp_path: Path) -> None:
    with closing(open_connection(tmp_path / "market.sqlite")) as conn:
        receipt_id = _retain(conn)
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close) VALUES ('1000','2026-10-16',100)"
        )
        with pytest.raises(TradingViewReceiptError, match="pending transaction"):
            _retain(conn)
        assert read_tool_results(conn, (receipt_id,))[0].receipt_id == receipt_id
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM jquants_daily_bars").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM tradingview_source_receipts").fetchone() == (1,)


def test_fixed_release_roundtrip_preserves_exact_receipt_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow_release_policy(monkeypatch, datasets=DATASETS, carries_history=None)
    source_path = market_store(tmp_path / "source.sqlite")
    with closing(open_connection(source_path)) as conn:
        first = _retain(conn)
        second = _retain(conn)
    expected = _rows(source_path)
    contract = LAKE_DATASETS["tradingview.source_receipts"]
    assert contract.layer == "l1_source"
    assert contract.coverage_authority == "unproven"
    assert LAKE_DATASETS["tradingview.forecast_snapshots"].layer == "l2_processed"
    from baibai_engine.storage.lake.models import PRODUCTION_RELEASE_POLICY

    policy = next(
        item for item in PRODUCTION_RELEASE_POLICY.datasets if item.dataset == contract.name
    )
    assert not policy.required
    assert policy.carries_history
    assert not policy.require_complete_coverage
    mirror = tmp_path / "mirror"
    built = datetime(2026, 10, 17, tzinfo=UTC)
    with sealed_sqlite_snapshot(sqlite_path=source_path, mirror_root=mirror) as snapshot:
        manifests = [
            export_dataset_snapshot(
                dataset_name=name,
                mirror_root=mirror,
                source_snapshot=snapshot,
                producer_git_commit="a" * 40,
                build_id=f"tv-source-{name.replace('.', '-')}",
                created_at=built,
            ).manifest_path
            for name in DATASETS
        ]
    manifest_path, manifest = create_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id="tradingview-source-fixed",
        created_at=built,
    )
    source = LocalMirrorSource(mirror)
    release = resolve_release(
        source, manifest.release_id, manifest_sha256=sha256_file(manifest_path)
    )
    destination = tmp_path / "hydrated.sqlite"
    shutil.copyfile(source_path, destination)
    with open_connection(destination) as conn:
        conn.execute("DELETE FROM tradingview_source_receipts")
        write_lake_store_origin(conn, LakeStoreOrigin(release.release_id, release.manifest_sha256))
        conn.commit()
    cache = LakeObjectCache(root=tmp_path / "cache", source=source)
    with lake_session() as lake:
        report = hydrate_market_store(
            lake, release=release, cache=cache, store=destination, dataset_names=DATASETS
        )
    assert report.rows["tradingview.source_receipts"] == 2
    assert _rows(destination) == expected
    with (
        closing(open_connection(destination)) as hydrated,
        closing(open_connection(source_path)) as original,
    ):
        assert read_tool_results(hydrated, (second, first)) == read_tool_results(
            original, (second, first)
        )
