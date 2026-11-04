from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from baibai_engine.data_collection.providers.jquants.lake_receipts import iter_retained_requests
from baibai_engine.data_collection.providers.jquants.snapshot_contracts import JQUANTS_SOURCE_PAGES


def _page(request: str, index: int, query: dict[str, str], body: dict[str, object], clock: str):
    return {
        "request_id": request,
        "page_index": index,
        "source": "daily_bars",
        "query_json": json.dumps(query),
        "received_at_utc": clock,
        "received_on": clock[:10],
        "body_gzip": gzip.compress(json.dumps(body).encode(), mtime=0),
    }


def _write(path: Path, rows: list[dict[str, object]]) -> Path:
    pq.write_table(pa.Table.from_pylist(rows, schema=JQUANTS_SOURCE_PAGES.arrow_schema), path)
    return path


def test_retained_pages_reconstruct_complete_and_report_incomplete(tmp_path: Path) -> None:
    clock = "2026-11-01T00:00:00.000000+00:00"
    query = {"date": "2026-10-31"}
    rows = [
        _page("a", 0, query, {"data": [{"code": "A"}], "pagination_key": "next"}, clock),
        _page("a", 1, {**query, "pagination_key": "next"}, {"data": [{"code": "B"}]}, clock),
        _page("b", 0, query, {"data": [], "pagination_key": "missing"}, clock),
        _page("c", 0, query, {"data": []}, clock),
    ]
    path = _write(tmp_path / "source.parquet", rows)
    with duckdb.connect(":memory:") as connection:
        actual = list(
            iter_retained_requests(
                connection, [path], source="daily_bars", known_at=datetime(2026, 11, 1, tzinfo=UTC)
            )
        )
    assert [x.request_id for x in actual] == ["a", "b", "c"]
    assert actual[0].request is not None
    assert actual[0].request.records() == [
        {"code": "A"},
        {"code": "B"},
    ]
    assert actual[1].request is None
    assert actual[1].error
    assert actual[1].first_query == query
    assert actual[1].last_received_at == datetime(2026, 11, 1, tzinfo=UTC)
    assert actual[2].request is not None
    assert actual[2].request.records() == []


def test_cutoff_is_utc_and_does_not_complete_later_page(tmp_path: Path) -> None:
    query = {"date": "2026-11-01"}
    path = _write(
        tmp_path / "source.parquet",
        [
            _page(
                "a",
                0,
                query,
                {"data": [], "pagination_key": "next"},
                "2026-11-01T00:00:00.000000+00:00",
            ),
            _page(
                "a",
                1,
                {**query, "pagination_key": "next"},
                {"data": []},
                "2026-11-01T00:01:00.000000+00:00",
            ),
        ],
    )
    with duckdb.connect(":memory:") as connection:
        result = list(
            iter_retained_requests(
                connection,
                [path],
                source="daily_bars",
                known_at=datetime(2026, 11, 1, 9, 0, 30, tzinfo=timezone(timedelta(hours=9))),
            )
        )
    assert len(result) == 1
    assert result[0].request is None
    assert result[0].error


def test_rejects_naive_cutoff_and_nonlocal_paths(tmp_path: Path) -> None:
    with duckdb.connect(":memory:") as connection:
        with pytest.raises(ValueError, match="timezone"):
            list(
                iter_retained_requests(
                    connection,
                    [],
                    source="daily_bars",
                    known_at=datetime(2026, 11, 1),  # noqa: DTZ001
                )
            )
        with pytest.raises(ValueError, match="local Parquet"):
            list(
                iter_retained_requests(
                    connection,
                    [tmp_path / "missing"],
                    source="daily_bars",
                    known_at=datetime.now(UTC),
                )
            )


def test_missing_duplicate_and_invalid_pages_are_not_complete(tmp_path: Path) -> None:
    clock = "2026-11-01T00:00:00.000000+00:00"
    query = {"date": "2026-11-01"}
    missing = _page("missing", 1, query, {"data": []}, clock)
    duplicate = _page("duplicate", 0, query, {"data": []}, clock)
    invalid = _page("invalid", 0, query, {"data": []}, clock)
    invalid["body_gzip"] = b"not a gzip stream"
    first = _write(tmp_path / "a.parquet", [missing, duplicate, invalid])
    second = _write(tmp_path / "b.parquet", [duplicate])
    with duckdb.connect(":memory:") as connection:
        result = list(
            iter_retained_requests(
                connection,
                [first, second],
                source="daily_bars",
                known_at=datetime(2026, 11, 2, tzinfo=UTC),
            )
        )
    assert {x.request_id for x in result} == {"missing", "duplicate", "invalid"}
    assert all(x.request is None and x.error for x in result)


def test_cutoff_compares_actual_instant_not_timestamp_text(tmp_path: Path) -> None:
    rows = [_page("a", 0, {"date": "2026-11-01"}, {"data": []}, "2026-11-01T09:00:00+09:00")]
    path = _write(tmp_path / "source.parquet", rows)
    with duckdb.connect(":memory:") as connection:
        result = list(
            iter_retained_requests(
                connection,
                [path],
                source="daily_bars",
                known_at=datetime(2026, 11, 1, 0, 1, tzinfo=UTC),
            )
        )
    assert len(result) == 1
    assert result[0].request is not None


def test_mixed_source_pages_are_not_silently_filtered(tmp_path: Path) -> None:
    clock = "2026-11-01T00:00:00.000000+00:00"
    query = {"date": "2026-11-01"}
    first = _page("same", 0, query, {"data": []}, clock)
    conflicting = _page("same", 1, query, {"data": []}, clock)
    conflicting["source"] = "master"
    path = _write(tmp_path / "source.parquet", [first, conflicting])
    with duckdb.connect(":memory:") as connection:
        result = list(
            iter_retained_requests(
                connection, [path], source="daily_bars", known_at=datetime(2026, 11, 2, tzinfo=UTC)
            )
        )
    assert len(result) == 1
    assert result[0].request is None
    assert result[0].error
