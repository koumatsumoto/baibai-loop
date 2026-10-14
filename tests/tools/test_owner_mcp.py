from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import Client
from tests.helpers.candidates import published_review_set
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tools.l1_mcp.contract import LIMITS
from tools.l1_mcp.server import Adapter
from tools.owner_mcp.reader import (
    OwnerError,
    Reader,
    ReviewSetSelector,
    digest,
)
from tools.owner_mcp.server import call, create_server

from baibai_engine.screening.discovery.contracts import PublishedReviewSet
from baibai_engine.screening.discovery.projection import review_set_candidates
from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION, SCHEMA_SQL
from baibai_engine.storage.application.write import initialize_database


def seed(
    reader,
    *,
    identifier="triage-a",
    day="2026-07-19",
    published_at=None,
    assessment_id=None,
    triaged=True,
):
    review = PublishedReviewSet.model_validate(
        published_review_set(
            as_of=day, review_set_id=f"review-{identifier}", run_revision_id=f"run-{identifier}"
        )
    )
    with sqlite3.connect(reader.runs_path) as con:
        con.execute(
            "INSERT INTO screening_run VALUES (?,?,?,?,?,?,?,?,?)",
            (
                review.run_revision_id,
                identifier,
                day,
                day,
                review.created_at.isoformat(),
                1,
                "rules.yaml",
                review.created_at.isoformat(),
                json.dumps({"screening_rules_hash": review.screening_rules_hash}),
            ),
        )
        con.execute(
            "INSERT INTO review_set VALUES (?,?,?,?)",
            (
                review.review_set_id,
                review.run_revision_id,
                review.created_at.isoformat(),
                review.model_dump_json(),
            ),
        )
    return None, review


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
                "screening_get_review_set",
                "data_catalog",
                "data_list",
                "data_get",
            }
            for tool in tools.tools:
                assert tool.annotations.read_only_hint is True
                assert tool.annotations.destructive_hint is False
                assert tool.annotations.open_world_hint is False
            got = await client.call_tool("screening_get_review_set", {})
            assert not got.is_error
        async with Client(create_server(adapter, reader)) as client:
            repeated = await client.call_tool("screening_get_review_set", {})
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


def test_review_set_selectors_parity_and_untriaged(reader):
    _, old = seed(reader)
    _, new = seed(reader, identifier="new", day="2026-07-20", triaged=False)
    before = [p.read_bytes() for p in (reader.app_path, reader.runs_path)]
    old_result = reader.get_review_set(ReviewSetSelector(review_set_id=old.review_set_id))
    assert old_result["candidates"] == list(review_set_candidates(old))
    for selector in (
        {},
        {"public_run_id": "new"},
        {"as_of": "2026-07-20"},
        {"not_before": "2026-07-20"},
    ):
        result = reader.get_review_set(ReviewSetSelector(**selector))
        assert result["review_set_ref"] == {
            "review_set_id": new.review_set_id,
            "run_revision_id": new.run_revision_id,
            "public_run_id": "new",
        }
        assert result["candidate_count"] == len(new.entries)
        assert result["input_basis"] == "frozen_review_set"
        assert reader.get_review_set(ReviewSetSelector(review_set_id=new.review_set_id)) == result
    assert reader.get_review_set(ReviewSetSelector(not_before="2026-07-18")) == old_result
    assert before == [p.read_bytes() for p in (reader.app_path, reader.runs_path)]


def test_review_set_frozen_despite_changed_run_analysis_and_missing_app(reader):
    _, review = seed(reader)
    selector = ReviewSetSelector(review_set_id=review.review_set_id)
    original = reader.get_review_set(selector)
    with sqlite3.connect(reader.runs_path) as con:
        # Current Security Analysis is deliberately unusable as a candidate projection.
        con.execute(
            "INSERT INTO security_analysis (run_revision_id,ticker,ordinal,sector_33,payload) VALUES (?,?,?,?,?)",
            (
                review.run_revision_id,
                "2331",
                0,
                "Company",
                '{"fcf_ttm":999999}',
            ),
        )
    reader.app_path.unlink()
    assert reader.get_review_set(selector) == original
    assert (
        "ttm_quality_fcf" not in original["candidates"][0]["snapshot"]["analysis"]["data_quality"]
    )


@pytest.mark.parametrize(
    "selector",
    [
        {"review_set_id": "missing"},
        {"public_run_id": "missing"},
        {"as_of": "2026-07-18"},
        {"not_before": "2026-07-21"},
    ],
)
def test_review_set_not_found_never_falls_back(reader, selector):
    seed(reader)
    with pytest.raises(OwnerError, match="SOURCE_UNAVAILABLE"):
        reader.get_review_set(ReviewSetSelector(**selector))


def test_review_set_daily_latest_and_ambiguous_public_run(reader):
    from baibai_engine.screening.run_store import ScreeningRunReader

    _, first = seed(reader)
    _, second = seed(reader, identifier="triage-b", triaged=False)
    # UTC serialization sorts lexically before +09:00 but is a later instant.
    payload = second.model_dump(mode="json")
    payload["created_at"] = "2026-07-19T05:00:00+00:00"
    with sqlite3.connect(reader.runs_path) as con:
        con.execute(
            "UPDATE review_set SET created_at=?, payload=? WHERE review_set_id=?",
            (
                payload["created_at"],
                json.dumps(payload),
                second.review_set_id,
            ),
        )
        con.execute(
            "UPDATE screening_run SET public_run_id='triage-a', run_at=? WHERE run_revision_id=?",
            (
                payload["created_at"],
                second.run_revision_id,
            ),
        )
    for selector in ({}, {"as_of": "2026-07-19"}, {"not_before": "2026-07-19"}):
        assert (
            reader.get_review_set(ReviewSetSelector(**selector))["review_set_ref"]["review_set_id"]
            == second.review_set_id
        )
    assert (
        ScreeningRunReader(reader.runs_path)
        .latest_review_set(as_of_date="2026-07-19")
        .review_set_id
        == second.review_set_id
    )
    with pytest.raises(OwnerError, match="AMBIGUOUS_SELECTION"):
        reader.get_review_set(ReviewSetSelector(public_run_id="triage-a"))
    assert reader.get_review_set(ReviewSetSelector(review_set_id=first.review_set_id))


def test_review_set_mcp_validation_and_parity(reader):
    seed(reader)
    adapter = Adapter()

    async def check():
        async with Client(create_server(adapter, reader)) as client:
            for args in (
                {"review_set_id": "review-triage-a", "not_before": "2026-07-19"},
                {"as_of": "2026-02-30"},
                {"not_before": "20260719"},
                {"review_set_id": ""},
            ):
                result = await client.call_tool("screening_get_review_set", args)
                assert result.is_error
                assert result.structured_content["error"]["code"] == "INVALID_ARGUMENT"
            result = await client.call_tool("screening_get_review_set", {})
            assert not result.is_error
            assert (
                result.structured_content["candidates"]
                == reader.get_review_set(ReviewSetSelector())["candidates"]
            )

    try:
        asyncio.run(check())
    finally:
        adapter.close()
