from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import Client
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tools.l1_mcp.contract import LIMITS
from tools.l1_mcp.server import Adapter
from tools.owner_mcp.reader import (
    CandidateSelector,
    OwnerError,
    Reader,
    digest,
)
from tools.owner_mcp.server import call, create_server

from baibai_engine.screening.discovery.projection import candidate_rows
from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION, SCHEMA_SQL
from baibai_engine.storage.application.write import initialize_database


def seed(reader, *, identifier="run-a", day="2026-07-19", published_at=None, rows=None):
    from tests.engine.test_candidate_discovery import _analysis
    from tests.engine.test_screening_run_store import _publish, _run

    from baibai_engine.screening.run_store import ScreeningRunReader

    _publish(
        reader.runs_path,
        _run(
            rows=[_analysis("2331")] if rows is None else rows,
            asof=day,
            at=published_at or f"{day}T13:00:00+09:00",
        ),
        revision=identifier,
    )
    return None, ScreeningRunReader(reader.runs_path).get_run(identifier)


@pytest.fixture
def reader(tmp_path):
    reader = Reader(tmp_path / "app.sqlite", tmp_path / "runs.sqlite")
    initialize_database(reader.app_path)
    with sqlite3.connect(reader.runs_path) as con:
        con.executescript(SCHEMA_SQL)
        con.execute(f"PRAGMA user_version={RUN_STORE_SCHEMA_VERSION}")
    return reader


def ledger(reader):
    document = load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    document = document.model_copy(update={"market_prices": ()})
    seed_ledger(reader.app_path, document)
    return document


@pytest.mark.parametrize(
    ("at", "held", "reserved"),
    [
        ("2026-06-01T10:00:00+09:00", [], []),
        ("2026-06-02T10:00:00+09:00", [], ["2331"]),
        ("2026-06-03T10:00:00+09:00", ["2331"], ["2331"]),
        ("2026-06-05T15:30:00+09:00", ["2331"], []),
        ("2026-07-04T10:00:00+09:00", ["2331"], ["8929"]),
    ],
)
def test_portfolio_point_in_time_and_minimal_disclosure(reader, at, held, reserved):
    ledger(reader)
    result = reader.exclusions(at)
    assert result["held_tickers"] == held
    assert result["reserved_tickers"] == reserved
    assert result["exclude_tickers"] == sorted(set(held + reserved))
    assert set(result) == {
        "at",
        "ledger_as_of",
        "held_tickers",
        "reserved_tickers",
        "exclude_tickers",
        "exclusion_sha256",
    }
    assert result == reader.exclusions(at)
    assert result["exclusion_sha256"] == digest(
        {"at": at, "held_tickers": held, "reserved_tickers": reserved}
    )


def test_portfolio_coverage_unimported_corrupt_and_unresolved(reader):
    with pytest.raises(OwnerError, match="PORTFOLIO_UNAVAILABLE"):
        reader.exclusions("2026-07-01T00:00:00+09:00")
    document = ledger(reader)
    with pytest.raises(OwnerError, match="PORTFOLIO_COVERAGE_INSUFFICIENT"):
        reader.exclusions((document.as_of + timedelta(seconds=1)).isoformat())
    with sqlite3.connect(reader.app_path) as con:
        row = con.execute(
            "SELECT payload FROM ledger_event WHERE event_id='release-2331-expired'"
        ).fetchone()
        payload = json.loads(row[0])
        payload["occurred_at"] = "2026-06-06T15:30:00+09:00"
        con.execute(
            "UPDATE ledger_event SET occurred_at=?,payload=? WHERE event_id='release-2331-expired'",
            (payload["occurred_at"], json.dumps(payload)),
        )
    with pytest.raises(OwnerError, match="PORTFOLIO_UNRESOLVED"):
        reader.exclusions("2026-06-05T16:00:00+09:00")
    with sqlite3.connect(reader.app_path) as con:
        con.execute("DELETE FROM ledger_meta")
    assert (
        call(lambda: reader.exclusions(document.as_of.isoformat())).structured_content["error"][
            "code"
        ]
        == "CONTRACT_MISMATCH"
    )


def test_public_surface_sanitized_errors_and_result_limit(reader):
    seed(reader)
    adapter = Adapter()

    async def check():
        async with Client(create_server(adapter, reader)) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "l1_resolve_current",
                "l1_describe_dataset",
                "l1_query",
                "portfolio_get_exclusions",
                "screening_get_candidates",
                "data_catalog",
                "data_list",
                "data_get",
            }
            for tool in tools.tools:
                assert tool.annotations.read_only_hint is True
                assert tool.annotations.destructive_hint is False
                assert tool.annotations.open_world_hint is False
            got = await client.call_tool("screening_get_candidates", {})
            assert not got.is_error
        async with Client(create_server(adapter, reader)) as client:
            repeated = await client.call_tool("screening_get_candidates", {})
            assert repeated.structured_content == got.structured_content

    try:
        asyncio.run(check())
    finally:
        adapter.close()
    assert (
        call(lambda: {"large": "x" * LIMITS.result_bytes}).structured_content["error"]["code"]
        == "RESULT_TOO_LARGE"
    )

    def broken():
        raise RuntimeError("SECRET /private/path")

    assert "SECRET" not in call(broken).model_dump_json()
    assert "private/path" not in call(broken).model_dump_json()
    assert (
        call(lambda: reader.exclusions("2026-07-19T10:00:00")).structured_content["error"]["code"]
        == "INVALID_ARGUMENT"
    )


def test_candidates_latest_daily_exact_parity_and_app_independence(reader):
    _, old = seed(reader)
    _, new = seed(reader, identifier="new", day="2026-07-20")
    before = [p.read_bytes() for p in (reader.app_path, reader.runs_path)]
    old_result = reader.get_candidates(CandidateSelector(run_revision_id=old.run_revision_id))
    assert old_result["candidates"] == list(candidate_rows(old.stock_analyses))
    for selector in ({}, {"as_of": "2026-07-20"}, {"run_revision_id": "new"}):
        result = reader.get_candidates(CandidateSelector(**selector))
        assert result["run_revision_id"] == new.run_revision_id
        assert result["count"] == 1
        assert result["input_basis"] == "saved_stock_analyses"
        assert "review_set_ref" not in result
        assert "created_at" not in result
    assert before == [p.read_bytes() for p in (reader.app_path, reader.runs_path)]
    reader.app_path.unlink()
    assert (
        reader.get_candidates(CandidateSelector(run_revision_id=old.run_revision_id)) == old_result
    )


@pytest.mark.parametrize("selector", [{"run_revision_id": "missing"}, {"as_of": "2026-07-18"}])
def test_candidates_not_found_never_falls_back(reader, selector):
    seed(reader)
    with pytest.raises(OwnerError, match="not_found"):
        reader.get_candidates(CandidateSelector(**selector))


def test_candidates_zero_is_success_and_daily_multiple_revisions_use_latest(reader):
    from tests.engine.test_screening_run_store import _publish, _run

    seed(reader)
    _publish(
        reader.runs_path,
        _run(rows=[], asof="2026-07-19", at="2026-07-19T05:00:00+00:00"),
        revision="latest-empty",
    )
    result = reader.get_candidates(CandidateSelector(as_of="2026-07-19"))
    assert result["run_revision_id"] == "latest-empty"
    assert result["candidates"] == []
    assert result["count"] == 0
    assert reader.get_candidates(CandidateSelector()) == result
    assert reader.get_candidates(CandidateSelector(run_revision_id="run-a"))["count"] == 1


def test_candidates_mcp_validation_and_parity(reader):
    seed(reader)
    adapter = Adapter()

    async def check():
        async with Client(create_server(adapter, reader)) as client:
            for args in (
                {"run_revision_id": "run-a", "as_of": "2026-07-19"},
                {"as_of": "2026-02-30"},
                {"as_of": "20260719"},
                {"run_revision_id": ""},
            ):
                result = await client.call_tool("screening_get_candidates", args)
                assert result.is_error
                assert result.structured_content["error"]["code"] == "INVALID_ARGUMENT"
            result = await client.call_tool("screening_get_candidates", {})
            assert not result.is_error
            assert result.structured_content == reader.get_candidates(CandidateSelector())

    try:
        asyncio.run(check())
    finally:
        adapter.close()
