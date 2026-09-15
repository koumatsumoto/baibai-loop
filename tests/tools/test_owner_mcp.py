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
from tests.helpers.macro_context import macro_context_payload
from tests.helpers.research_triage import published_review_set, research_triage_payload
from tools.l1_mcp.contract import LIMITS
from tools.l1_mcp.server import Adapter
from tools.owner_mcp.reader import OwnerError, Reader, Selector, TriageRef, digest
from tools.owner_mcp.server import call, create_server

from baibai_batch.analysis.model_input import build_model_input
from baibai_engine.appdb.write import initialize_database
from baibai_engine.foundation.research_triage import ResearchTriage
from baibai_engine.macro.context.models import MacroContextDocument
from baibai_engine.macro.context.service import MacroContextService
from baibai_engine.screening.discovery.review_set import PublishedReviewSet
from baibai_engine.screening.research_triage import ResearchTriageService, latest_research_triage_id
from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION, SCHEMA_SQL


def seed(reader, *, identifier="triage-a", day="2026-07-19", published_at=None, macro=None):
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
    triage = ResearchTriage.model_validate(
        research_triage_payload(
            research_triage_id=identifier,
            review_set_id=review.review_set_id,
            run_revision_id=review.run_revision_id,
            as_of=day,
            published_at=published_at,
            macro_context_id=macro,
            expected_prior_research_triage_id=latest_research_triage_id(reader.app_path),
        )
    )
    return ResearchTriageService(reader.app_path).publish(triage, review_set=review), review


@pytest.fixture
def reader(tmp_path):
    reader = Reader(tmp_path / "app.sqlite", tmp_path / "runs.sqlite")
    initialize_database(reader.app_path)
    with sqlite3.connect(reader.runs_path) as con:
        con.executescript(SCHEMA_SQL)
        con.execute(f"PRAGMA user_version={RUN_STORE_SCHEMA_VERSION}")
    return reader


def ref(reader, **selector):
    return TriageRef.model_validate(reader.resolve(Selector(**selector))["triage_ref"])


def test_fixed_input_and_full_judgment_across_clients(reader):
    triage, review = seed(reader)
    before = [p.read_bytes() for p in (reader.app_path, reader.runs_path)]
    reference = ref(reader, research_triage_id=triage.research_triage_id)
    result = reader.get_input(reference)
    expected = build_model_input(review, None).model_dump(mode="json")
    assert result["model_input"] == expected
    assert reference.model_input_sha256 == digest(expected)
    assert result["input_basis"] == "reconstructed_from_production_contract"
    assert reader.get_judgment(reference)["research_triage"] == triage.model_dump(mode="json")
    assert Reader(reader.app_path, reader.runs_path).get_input(reference) == result
    forbidden = {"decision", "priority", "rationale", "research_question", "key_risk"}

    def keys(value):
        if isinstance(value, dict):
            return set(value).union(*(keys(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(v) for v in value))
        return set()

    assert not forbidden.intersection(keys(result))
    assert before == [p.read_bytes() for p in (reader.app_path, reader.runs_path)]


def test_exact_macro_and_null_binding_survive_later_context(reader):
    seed(reader)
    missing_ref = ref(reader)
    service = MacroContextService(reader.app_path)
    a = MacroContextDocument.model_validate(
        macro_context_payload(context_id="macro-context-2026-07-19-a")
    )
    service.publish(a, expected_head=None)
    _, review = seed(
        reader, identifier="triage-b", published_at="2026-07-19T15:00:00+09:00", macro=a.context_id
    )
    bound_ref = ref(reader)
    b = MacroContextDocument.model_validate(
        macro_context_payload(
            context_id="macro-context-2026-07-19-b", published_at="2026-07-19T16:00:00+09:00"
        )
    ).model_copy(update={"summary": "後発context"})
    service.publish(b, expected_head=a.context_id)
    assert reader.get_input(missing_ref)["model_input"]["macro_context"]["status"] == "missing"
    assert reader.get_input(bound_ref)["model_input"] == build_model_input(review, a).model_dump(
        mode="json"
    )


def test_policy_drift_refuses_input_but_not_judgment(reader, monkeypatch):
    import baibai_batch.analysis.model_input as builder

    triage, _ = seed(reader)
    reference = ref(reader)
    monkeypatch.setattr(builder, "TRIAGE_POLICY", (*builder.TRIAGE_POLICY, "Changed policy"))
    with pytest.raises(OwnerError, match="REFERENCE_MISMATCH"):
        reader.get_input(reference)
    assert reader.get_judgment(reference)["research_triage"] == triage.model_dump(mode="json")
    for name in ("get_input", "get_judgment"):
        with pytest.raises(OwnerError, match="REFERENCE_MISMATCH"):
            getattr(reader, name)(reference.model_copy(update={"research_triage_sha256": "f" * 64}))


def test_resolve_order_ambiguity_and_pruned_source(reader):
    first, _ = seed(reader)
    second, _ = seed(reader, identifier="triage-b", published_at="2026-07-19T16:00:00+09:00")
    third, _ = seed(reader, identifier="triage-c", day="2026-07-20")
    assert ref(reader).research_triage_id == third.research_triage_id
    assert ref(reader, not_before="2026-07-19").research_triage_id == first.research_triage_id
    with pytest.raises(OwnerError, match="AMBIGUOUS_SELECTION"):
        ref(reader, as_of="2026-07-19")
    with sqlite3.connect(reader.runs_path) as con:
        con.execute("DELETE FROM review_set WHERE review_set_id=?", (first.review_set_id,))
    with pytest.raises(OwnerError, match="SOURCE_UNAVAILABLE"):
        ref(reader, research_triage_id=first.research_triage_id)
    assert ref(reader, not_before="2026-07-19").research_triage_id == second.research_triage_id
    assert ref(reader, as_of="2026-07-19").research_triage_id == second.research_triage_id
    with pytest.raises(OwnerError, match="SOURCE_UNAVAILABLE"):
        ref(reader, as_of="2026-07-18")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("review_set_id", "different"),
        ("run_revision_id", "different"),
        ("as_of", "2026-07-18"),
        ("screening_rules_hash", "f" * 64),
        ("method", {"method_id": "different", "method_hash": "f" * 64, "nomination_depth": 20}),
        ("entries", []),
    ],
)
def test_source_binding_mismatch_does_not_fallback(reader, field, value):
    triage, review = seed(reader)
    payload = review.model_dump(mode="json")
    payload[field] = value
    with sqlite3.connect(reader.runs_path) as con:
        con.execute("UPDATE review_set SET payload=?", (json.dumps(payload),))
    result = call(lambda: reader.resolve(Selector(research_triage_id=triage.research_triage_id)))
    assert result.is_error
    assert result.structured_content["error"]["code"] == "CONTRACT_MISMATCH"


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
                "triage_resolve",
                "triage_get_input",
                "triage_get_judgment",
                "portfolio_get_exclusions",
            }
            for tool in tools.tools:
                assert tool.annotations.read_only_hint is True
                assert tool.annotations.destructive_hint is False
                assert tool.annotations.open_world_hint is False
            for args in (
                {"as_of": "2026-02-30"},
                {"as_of": "2026-07-19", "not_before": "2026-07-19"},
            ):
                result = await client.call_tool("triage_resolve", args)
                assert result.is_error
            resolved = await client.call_tool("triage_resolve", {})
            reference = resolved.structured_content["triage_ref"]
            got = await client.call_tool("triage_get_input", {"triage_ref": reference})
            assert not got.is_error
        async with Client(create_server(adapter, reader)) as client:
            repeated = await client.call_tool("triage_get_input", {"triage_ref": reference})
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
