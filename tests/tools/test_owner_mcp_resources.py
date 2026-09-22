from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from mcp import Client
from tests.helpers.calibration_store import publish_forward, publish_panel
from tests.helpers.db_seed import seed_ledger, seed_tasks
from tests.helpers.ledger import load_portfolio_ledger
from tests.tools.test_owner_mcp import reader as reader  # shared source fixtures
from tests.tools.test_owner_mcp import seed
from tools.l1_mcp.contract import LIMITS, wire
from tools.l1_mcp.server import Adapter
from tools.owner_mcp.resource_types import Paths
from tools.owner_mcp.resources import Resources, decode_cursor, result_size
from tools.owner_mcp.server import call, create_server

from baibai_engine.macro.indicators.db import ObservationRecord, insert_observations
from baibai_engine.macro.indicators.db import initialize_database as macro_init
from baibai_engine.market.lake.datasets import LAKE_DATASETS
from baibai_engine.read_api.macro import macro_reading_snapshot
from baibai_engine.tasks.models import Task


@pytest.fixture
def data(reader, tmp_path):
    return Resources(
        Paths(
            application=reader.app_path,
            runs=reader.runs_path,
            macro=tmp_path / "macro.sqlite",
            market=tmp_path / "market.sqlite",
            calibration=tmp_path / "calibration/current.sqlite",
            er=tmp_path / "er.yaml",
        )
    )


def error(action, code):
    result = call(action)
    assert result.is_error
    assert result.structured_content["error"]["code"] == code
    return result


def pages(data, resource, filters=None, limit=2):
    result = data.list(resource, filters, limit)
    rows = result["items"][:]
    while result["next_cursor"]:
        result = data.list(resource, cursor=result["next_cursor"], limit=limit)
        rows.extend(result["items"])
    return rows


def test_catalog_is_complete_without_stores_and_schemas_are_live(data):
    expected = {
        "macro.series",
        "macro.observation",
        "macro.provider_run",
        "macro.reading",
        "macro.context",
        "market.source_coverage",
        "market.capital_policy_snapshot",
        "screening.run",
        "screening.security_analysis",
        "screening.review_set",
        "screening.calibration.cohort",
        "screening.calibration.panel_row",
        "screening.calibration.forward_row",
        "screening.er_calibration_context",
        "research.triage",
        "research.thesis",
        "research.thesis_review",
        "research.capital_allocation_assessment",
        "position.review",
        "portfolio.ledger",
        "portfolio.ledger_event",
        "portfolio.market_price",
        "portfolio.outcome",
        "task",
        "operation.session",
    }
    assert set(data.specs) == expected | {f"market.{name}" for name in LAKE_DATASETS}
    for name, spec in data.specs.items():
        detail = data.catalog(name)
        assert detail["filter_schema"] == spec.filters.model_json_schema()
        assert detail["selector_schema"] == spec.selector.model_json_schema()
    assert (
        data.catalog(layer="L2", domain="macro")["resources"][0]["resource_id"] == "macro.reading"
    )
    assert not data.paths.macro.exists()
    error(lambda: data.catalog("macro.reading", layer="L2"), "INVALID_ARGUMENT")
    error(lambda: data.catalog(domain="../"), "INVALID_ARGUMENT")


@pytest.mark.parametrize(
    ("resource", "filters"),
    [
        ("unknown", {}),
        ("macro.series", {"sql": "DROP TABLE series"}),
        ("macro.series", {"path": "/etc/passwd"}),
        ("macro.observation", {"series_id": "jp.10y", "from": "2026-02-30", "to": "2026-03-01"}),
        (
            "macro.observation",
            {
                "series_id": "jp.10y",
                "from": "2026-02-01",
                "to": "2026-03-01",
                "as_of": "2026-02-20",
            },
        ),
        (
            "macro.observation",
            {
                "series_id": "jp.10y",
                "from": "2026-02-01",
                "to": "2026-03-01",
                "mode": "vintages",
                "as_of": "2026-03-01",
            },
        ),
        ("task", {"status": "invalid"}),
        ("portfolio.ledger_event", {"event_type": "guess"}),
        ("screening.calibration.forward_row", {"asof": "2026-01-01", "horizon": "1d"}),
    ],
)
def test_invalid_resource_filters(data, resource, filters):
    error(lambda: data.list(resource, filters), "INVALID_ARGUMENT")


@pytest.mark.parametrize("limit", [True, False, 0, -1, 2001, 1.0, "1"])
def test_strict_limit(data, limit):
    error(lambda: data.list("macro.series", limit=limit), "INVALID_ARGUMENT")


def test_cursor_continuation_validates_resource_filters_and_key(data):
    first = data.list("macro.series", limit=1)
    assert first["next_cursor"]
    assert len(pages(data, "macro.series", limit=1)) == len(data.list("macro.series")["items"])
    error(lambda: data.list("macro.provider_run", cursor=first["next_cursor"]), "INVALID_ARGUMENT")
    error(
        lambda: data.list("macro.series", {"category": "different"}, cursor=first["next_cursor"]),
        "INVALID_ARGUMENT",
    )
    for raw in ["garbage", "e30=", "A" * 40000]:
        error(lambda raw=raw: data.list("macro.series", cursor=raw), "INVALID_ARGUMENT")
    obj = decode_cursor(first["next_cursor"]).model_dump()
    obj["after"] = [False]
    import base64

    cursor = base64.urlsafe_b64encode(wire(obj)).decode()
    error(lambda: data.list("macro.series", cursor=cursor), "INVALID_ARGUMENT")


def test_missing_unwritten_empty_and_corrupt_are_distinct(data):
    error(lambda: data.list("macro.provider_run"), "SOURCE_UNAVAILABLE")
    assert not data.paths.macro.exists()
    sqlite3.connect(data.paths.macro).close()
    error(lambda: data.list("macro.provider_run"), "SOURCE_UNAVAILABLE")
    macro_init(data.paths.macro).close()
    assert data.list("macro.provider_run")["items"] == []
    with sqlite3.connect(data.paths.macro) as con:
        con.execute("PRAGMA user_version=999")
    error(lambda: data.list("macro.provider_run"), "CONTRACT_MISMATCH")
    error(lambda: data.get("task", {"task_id": "missing"}), "SOURCE_UNAVAILABLE")


def test_run_header_analysis_paging_full_review_and_pruned_triage(data, reader, monkeypatch):
    triage, review = seed(reader)
    with sqlite3.connect(reader.runs_path) as con:
        for ordinal, ticker in enumerate(["1301", "1302", "1303", "1304"]):
            payload = {"ticker": ticker, "precise_saved": 0.123456789, "unknown_saved": None}
            con.execute(
                "INSERT INTO security_analysis(run_revision_id,ordinal,ticker,payload) VALUES(?,?,?,?)",
                (review.run_revision_id, ordinal, ticker, json.dumps(payload)),
            )
    from baibai_engine.screening.run_store.read import ScreeningRunReader

    monkeypatch.setattr(ScreeningRunReader, "get_run", lambda *_: pytest.fail("full run loaded"))
    header = data.get("screening.run", {"run_revision_id": review.run_revision_id})
    assert header["meta"]["payload_scope"] == "run_header"
    items = pages(data, "screening.security_analysis", {"run_revision_id": review.run_revision_id})
    assert [item["payload"]["ordinal"] for item in items] == [0, 1, 2, 3]
    assert items[-1]["payload"]["payload"]["precise_saved"] == 0.123456789
    exact = data.get("screening.security_analysis", items[-1]["selector"])
    assert (
        data.get("screening.security_analysis", resource_ref=exact["resource_ref"])["payload"]
        == exact["payload"]
    )
    assert data.get("screening.review_set", {"as_of": review.as_of.isoformat()})["payload"][
        "payload"
    ] == review.model_dump(mode="json")
    error(
        lambda: data.list("screening.security_analysis", {"run_revision_id": "missing"}),
        "SOURCE_UNAVAILABLE",
    )
    reader.runs_path.unlink()
    assert data.get("research.triage", {"research_triage_id": triage.research_triage_id})[
        "payload"
    ]["payload"] == triage.model_dump(mode="json")


def test_bytes_pagination_never_skips_unreturned_items(data, reader, monkeypatch):
    _, review = seed(reader)
    with sqlite3.connect(reader.runs_path) as con:
        for index in range(6):
            con.execute(
                "INSERT INTO security_analysis(run_revision_id,ordinal,ticker,payload) VALUES(?,?,?,?)",
                (
                    review.run_revision_id,
                    index,
                    str(1300 + index),
                    json.dumps({"text": "あ" * 200}),
                ),
            )
    import tools.owner_mcp.resources as module

    monkeypatch.setattr(module, "LIMITS", replace(LIMITS, result_bytes=2200))
    result = data.list(
        "screening.security_analysis", {"run_revision_id": review.run_revision_id}, limit=6
    )
    assert 0 < result["returned_count"] < 6
    assert result_size(result) <= 2200
    assert decode_cursor(result["next_cursor"]).after == [result["items"][-1]["payload"]["ordinal"]]
    assert (
        len(
            pages(
                data,
                "screening.security_analysis",
                {"run_revision_id": review.run_revision_id},
                limit=6,
            )
        )
        == 6
    )
    monkeypatch.setattr(module, "LIMITS", replace(LIMITS, result_bytes=100))
    error(
        lambda: data.list(
            "screening.security_analysis", {"run_revision_id": review.run_revision_id}
        ),
        "RESULT_TOO_LARGE",
    )


def test_macro_raw_effective_fixed_cutoff_reading_and_redaction(data):
    with macro_init(data.paths.macro) as con:
        insert_observations(
            con,
            [
                ObservationRecord(
                    series_id="us.10y",
                    observed_at=date(2026, 1, day),
                    value=value,
                    unit="percent",
                    source_url="https://example.com/?api_key=secret",
                    vintage_at=datetime(2026, 1, vintage, tzinfo=UTC),
                    fetch_status=status,
                )
                for day, vintage, status, value in [
                    (1, 2, "ok", 1),
                    (1, 3, "failed", 2),
                    (2, 3, "ok", 3),
                    (2, 4, "retracted", 3),
                    (3, 4, "ok", 4),
                ]
            ],
        )
    filters = {
        "series_id": "us.10y",
        "from": "2026-01-01",
        "to": "2026-01-03",
        "as_of": "2026-01-05",
    }
    effective = pages(data, "macro.observation", filters, limit=1)
    assert [row["payload"]["observed_at"] for row in effective] == ["2026-01-01", "2026-01-03"]
    raw = pages(
        data,
        "macro.observation",
        {**{k: v for k, v in filters.items() if k != "as_of"}, "mode": "vintages"},
        limit=1,
    )
    assert len(raw) == 5
    got = data.get("macro.observation", raw[0]["selector"])
    assert "secret" not in json.dumps(got)
    assert got["meta"]["redacted"] is True
    snapshot = data.get("macro.reading", {"as_of": "2026-01-05"})
    assert snapshot["payload"] == macro_reading_snapshot(data.paths.macro, asof=date(2026, 1, 5))
    assert (
        data.get("macro.reading", resource_ref=snapshot["resource_ref"])["payload"]
        == snapshot["payload"]
    )
    reference = snapshot["resource_ref"]
    reference["identity"]["rules_revision"] = "different"
    error(lambda: data.get("macro.reading", resource_ref=reference), "REFERENCE_MISMATCH")
    error(
        lambda: data.get("macro.reading", {"as_of": "2026-01-05", "rules_revision": "different"}),
        "INVALID_ARGUMENT",
    )


def test_provider_jst_boundary_offset_sort_and_read_only(data):
    con = macro_init(data.paths.macro)
    try:
        for key, stamp in [
            ("a", "2026-01-01T15:00:00+00:00"),
            ("b", "2026-01-02T00:01:00+09:00"),
            ("c", "2026-01-02T15:00:00+00:00"),
        ]:
            con.execute(
                "INSERT INTO provider_runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    "fred",
                    "us.10y",
                    "2026-01-01",
                    "2026-01-02",
                    stamp,
                    stamp,
                    "failed",
                    0,
                    "https://e.test/?token=secret",
                ),
            )
        con.commit()
        before = list(con.iterdump())
        items = pages(
            data, "macro.provider_run", {"from": "2026-01-02", "to": "2026-01-02"}, limit=1
        )
        assert [row["selector"]["run_id"] for row in items] == ["a", "b"]
        assert list(con.iterdump()) == before
    finally:
        con.close()


def test_calibration_token_replacement_readiness_and_no_copy(data, monkeypatch):
    directory = data.paths.calibration.parent
    publish_panel(directory, "2026-01-01", [{"ticker": "1301"}, {"ticker": "1302"}])
    monkeypatch.setattr(shutil, "copy2", lambda *_: pytest.fail("reader copied DB"))
    first = data.list("screening.calibration.cohort", limit=1)
    token = first["meta"]["snapshot_token"]
    assert (
        data.list(
            "screening.calibration.panel_row", {"asof": "2026-01-01", "snapshot_token": token}
        )["returned_count"]
        == 2
    )
    error(
        lambda: data.list(
            "screening.calibration.forward_row", {"asof": "2026-01-01", "snapshot_token": token}
        ),
        "SOURCE_UNAVAILABLE",
    )
    publish_forward(
        directory,
        "2026-01-01",
        [{"ticker": "1301", "horizon": "3y", "status": "unresolved_no_exit_price"}],
    )
    error(
        lambda: data.get(
            "screening.calibration.cohort", {"asof": "2026-01-01", "snapshot_token": token}
        ),
        "REFERENCE_MISMATCH",
    )
    forward = data.list("screening.calibration.forward_row", {"asof": "2026-01-01"})
    assert forward["items"][0]["payload"]["payload"]["resolved"] is False
    import baibai_engine.screening.calibration.read as owner

    original = owner.select_page

    def replaced(*args, **kwargs):
        rows = original(*args, **kwargs)
        target = data.paths.calibration.with_suffix(".next")
        target.write_bytes(data.paths.calibration.read_bytes())
        target.replace(data.paths.calibration)
        return rows

    monkeypatch.setattr(owner, "select_page", replaced)
    error(lambda: data.list("screening.calibration.cohort"), "REFERENCE_MISMATCH")


def test_task_ref_mutation_singleton_and_ledger_numeric_facts(data):
    task = Task(
        task_id="task-20260101-a",
        title="check",
        kind="ops",
        status="open",
        due_date=date(2026, 1, 1),
        created_at=date(2026, 1, 1),
    )
    seed_tasks(data.paths.application, [task])
    got = data.get("task", {"task_id": task.task_id})
    with sqlite3.connect(data.paths.application) as con:
        payload = task.model_dump(mode="json")
        payload["title"] = "changed"
        con.execute("UPDATE task SET payload=?", (json.dumps(payload),))
    error(lambda: data.get("task", resource_ref=got["resource_ref"]), "REFERENCE_MISMATCH")
    error(
        lambda: data.get("task", {"task_id": task.task_id}, got["resource_ref"]), "INVALID_ARGUMENT"
    )
    fixture = Path("tests/fixtures/portfolio-ledger/representative.yaml")
    document = load_portfolio_ledger(fixture)
    document = document.model_copy(
        update={
            "market_prices": tuple(
                price.model_copy(update={"source_kind": "market_api"})
                for price in document.market_prices
            )
        }
    )
    seed_ledger(data.paths.application, document)
    ledger = data.get("portfolio.ledger", {})
    assert ledger["resource_ref"]["identity"] == {}
    assert "append_head" in ledger["payload"]
    assert pages(data, "portfolio.ledger_event")


def test_protocol_three_tools_accept_objects_without_gateway(reader):
    async def check():
        adapter = Adapter()
        try:
            async with Client(create_server(adapter, reader)) as client:
                result = await client.call_tool(
                    "data_get", {"resource_id": "macro.series", "selector": {"series_id": "jp.10y"}}
                )
                assert not result.is_error
                repeated = await client.call_tool(
                    "data_get",
                    {
                        "resource_id": "macro.series",
                        "resource_ref": result.structured_content["resource_ref"],
                    },
                )
                assert (
                    repeated.structured_content["payload"] == result.structured_content["payload"]
                )
        finally:
            adapter.close()

    asyncio.run(check())


def test_cursor_rejects_invalid_day_key_before_store_read(data):
    from tools.owner_mcp.resources import encode_cursor

    cursor = encode_cursor(
        "macro.observation",
        {"series_id": "jp.10y", "from": "2026-01-01", "to": "2026-01-31"},
        ["2026-02-30", "2026-01-31T00:00:00Z"],
        {},
    )
    error(lambda: data.list("macro.observation", cursor=cursor), "INVALID_ARGUMENT")
    assert not data.paths.macro.exists()


def test_reading_list_carries_rules_revision_on_every_page(data):
    with macro_init(data.paths.macro):
        pass
    filters = {"as_of": "2026-01-05"}
    snapshot = macro_reading_snapshot(data.paths.macro, asof=date(2026, 1, 5))
    whole = data.list("macro.reading", filters)
    assert whole["meta"]["rules_revision"] == snapshot["rules_revision"]
    page = data.list("macro.reading", filters, limit=1)
    assert page["next_cursor"]
    second = data.list("macro.reading", cursor=page["next_cursor"], limit=1)
    for result in (page, second):
        assert result["meta"]["rules_revision"] == snapshot["rules_revision"]
        assert "rules_revision" not in result["items"][0]["selector"]


@pytest.mark.parametrize("selector", [{"latest": True}, {"review_set_id": "missing"}])
def test_review_set_unwritten_store_is_unavailable_but_partial_schema_is_corrupt(data, selector):
    data.paths.runs.unlink()
    with sqlite3.connect(data.paths.runs):
        pass
    error(lambda: data.get("screening.review_set", selector), "SOURCE_UNAVAILABLE")
    from baibai_engine.screening.run_store.schema import RUN_STORE_SCHEMA_VERSION

    with sqlite3.connect(data.paths.runs) as con:
        con.execute(f"PRAGMA user_version={RUN_STORE_SCHEMA_VERSION}")
        con.execute("CREATE TABLE screening_run(run_revision_id TEXT, asof_date TEXT)")
    error(lambda: data.get("screening.review_set", selector), "CONTRACT_MISMATCH")


@pytest.mark.parametrize(
    ("changes", "removed", "expected"),
    [
        ({}, "from", "from: 必須"),
        ({"secret-key": "secret-value"}, None, "未知field"),
        ({"from": "secret-value"}, None, "from: ISO日付が必要"),
        ({"series_id": {"secret-key": "secret-value"}}, None, "series_id: 文字列が必要"),
    ],
)
def test_public_argument_errors_identify_reason_without_exposing_input(
    data, changes, removed, expected
):
    filters = {"series_id": "us.10y", "from": "2026-01-01", "to": "2026-01-05", **changes}
    if removed:
        filters.pop(removed)
    result = error(lambda: data.list("macro.observation", filters), "INVALID_ARGUMENT")
    assert expected in result.content[0].text
    assert expected in result.structured_content["error"]["message"]
    serialized = result.model_dump_json()
    assert "secret-key" not in serialized
    assert "secret-value" not in serialized


def test_run_pagination_uses_chronological_cursor_for_mixed_offsets(data):
    from tests.helpers.screening_run import screening_run_payload

    from baibai_engine.screening.run_store import ScreeningRunStore

    store = ScreeningRunStore(data.paths.runs)
    for identity, instant in (
        ("offset-old", "2026-07-08T18:00:00+09:00"),
        ("offset-new", "2026-07-08T10:00:00+00:00"),
        ("offset-tie-z", "2026-07-08T19:00:00+09:00"),
    ):
        store.publish_run(
            screening_run_payload(as_of="2026-07-08", run_at=instant),
            run_revision_id=identity,
        )
    result = pages(data, "screening.run", {"from": "2026-07-08", "to": "2026-07-08"}, limit=1)
    assert [item["payload"]["run_revision_id"] for item in result] == [
        "offset-old",
        "offset-new",
        "offset-tie-z",
    ]
    assert all("page_time" not in item["payload"] for item in result)
