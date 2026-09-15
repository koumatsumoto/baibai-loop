from __future__ import annotations

import asyncio
import hashlib
import subprocess
from dataclasses import asdict, replace
from datetime import UTC, datetime

import pytest
import requests
from mcp import Client
from pydantic import ValidationError
from tests.helpers.l1_release import market_store
from tests.helpers.lake_policy import narrow_release_policy
from tools.l1_mcp.contract import LIMITS, L1Error, Query, ReleaseRef, wire
from tools.l1_mcp.gateway import ORIGIN, Gateway
from tools.l1_mcp.query import run_child
from tools.l1_mcp.server import Adapter, create_server

from baibai_engine.market.lake import models
from baibai_engine.market.lake.keys import current_l1_pointer_key
from baibai_engine.market.lake.models import canonical_lake_model_bytes
from baibai_engine.market.lake.release import L1ReleasePointer, create_l1_release
from baibai_engine.market.lake.writer import export_legacy_sqlite, sealed_sqlite_snapshot
from baibai_engine.market.sqlite import open_connection


@pytest.fixture
def lake(tmp_path, monkeypatch):
    datasets = ("jquants.daily_bars", "jquants.short_sale_reports", "jquants.market_calendar")
    narrow_release_policy(monkeypatch, datasets=datasets)
    # A narrow fixture permits partial calendar coverage to exercise consumer reporting.
    policy = models.PRODUCTION_RELEASE_POLICY
    monkeypatch.setattr(
        models,
        "PRODUCTION_RELEASE_POLICY",
        policy.model_copy(
            update={
                "datasets": tuple(
                    item.model_copy(
                        update={
                            "minimum_population_count": None,
                            "require_complete_coverage": False,
                        }
                    )
                    if item.dataset == "jquants.market_calendar"
                    else item
                    for item in policy.datasets
                ),
            }
        ),
    )
    database = market_store(tmp_path / "market.sqlite")
    connection = open_connection(database)
    connection.executemany(
        "INSERT INTO jquants_market_calendar(day,is_business_day) VALUES (?,?)",
        [("2025-01-05", 0), ("2026-01-05", 1), ("2026-01-20", 1)],
    )
    connection.commit()
    connection.close()
    mirror = tmp_path / "mirror"
    built = datetime(2026, 3, 1, tzinfo=UTC)
    with sealed_sqlite_snapshot(
        sqlite_path=database, mirror_root=mirror, snapshot_id="g0-test"
    ) as snapshot:
        manifests = [
            export_legacy_sqlite(
                dataset_name=name,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
                source_snapshot=snapshot,
                build_id="test-" + name.replace(".", "-"),
                created_at=built,
            ).manifest_path
            for name in datasets
        ]
    path, release = create_l1_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id="test-release",
        created_at=built,
    )
    pointer = L1ReleasePointer(
        release_id=release.release_id,
        manifest_key=path.relative_to(mirror).as_posix(),
        manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    current = mirror / current_l1_pointer_key()
    current.parent.mkdir(parents=True)
    current.write_bytes(canonical_lake_model_bytes(pointer))
    return mirror, pointer


@pytest.fixture
def adapter(lake, tmp_path):
    mirror, pointer = lake
    gateway = Gateway("TEST-ONLY-SECRET", tmp_path / "manifest-cache")
    reads = []

    def get(url, **kwargs):
        assert url in (ORIGIN + "/api/lake/current", ORIGIN + "/api/lake/object")
        assert kwargs["allow_redirects"] is False
        assert kwargs["headers"]["Authorization"] == "Bearer TEST-ONLY-SECRET"
        key = current_l1_pointer_key() if kwargs["params"] is None else kwargs["params"]["key"]
        reads.append(key)
        path = mirror / key
        response = requests.Response()
        response.status_code = 200 if path.is_file() else 404
        data = path.read_bytes() if path.is_file() else b""
        response.headers["Content-Length"] = str(len(data))
        response._content = data
        response._content_consumed = True
        return response

    gateway.session.get = get
    app = Adapter(source=gateway)
    yield (
        app,
        reads,
        ReleaseRef(release_id=pointer.release_id, manifest_sha256=pointer.manifest_sha256),
    )
    app.close()


def request(reference, **updates):
    return Query.model_validate(
        {
            "release_ref": reference,
            "sources": [
                {
                    "dataset": "jquants.daily_bars",
                    "alias": "prices",
                    "from": "2026-01-01",
                    "to": "2026-01-31",
                }
            ],
            "sql": "SELECT ticker, close FROM prices ORDER BY ticker, traded_at",
            **updates,
        }
    )


def test_current_describe_query_and_warm_fixed_release(adapter, lake):
    app, reads, ref = adapter
    resolved = app.call(app.resolve)
    assert not resolved.is_error
    assert resolved.structured_content["release_ref"] == ref.model_dump()
    description = app.describe(ref, "jquants.daily_bars")
    assert description["date_column"] == "traded_at"
    assert description["partition_grain"] == "month"
    assert description["primary_key"] == ["ticker", "traded_at"]
    # Remove current after resolution: named reads must remain tied to the same release.
    (lake[0] / current_l1_pointer_key()).unlink()
    query = request(ref)
    first = app.call(lambda: app.query(query))
    assert not first.is_error
    assert first.structured_content["rows"] == [["1301", 100.0], ["1301", 105.0], ["7203", 200.0]]
    before = list(reads)
    second = app.call(lambda: app.query(query))
    assert second.structured_content["rows"] == first.structured_content["rows"]
    assert reads == before
    assert reads.count(current_l1_pointer_key()) == 1
    assert all("/month=2/" not in key for key in reads if key.endswith(".parquet"))
    assert second.structured_content["transfer"]["gateway_gets"] == 0


def test_join_window_year_partition_and_partial_coverage(adapter):
    app, reads, ref = adapter
    sources = [
        {
            "dataset": "jquants.daily_bars",
            "alias": "p",
            "from": "2026-01-05",
            "to": "2026-01-20",
            "columns": ["ticker", "traded_at", "close"],
        },
        {
            "dataset": "jquants.market_calendar",
            "alias": "c",
            "from": "2026-01-01",
            "to": "2026-01-31",
        },
    ]
    result = app.call(
        lambda: app.query(
            request(
                ref,
                sources=sources,
                sql="""
        WITH joined AS (SELECT p.* FROM p JOIN c ON p.traded_at=c.day WHERE c.is_business_day=$day)
        SELECT ticker, traded_at, sum(close) OVER (PARTITION BY ticker ORDER BY traded_at)
        FROM joined ORDER BY ticker,traded_at""",
                parameters={"day": 1},
            )
        )
    )
    assert not result.is_error
    assert result.structured_content["rows"] == [
        ["1301", "2026-01-05", 100.0],
        ["1301", "2026-01-20", 205.0],
        ["7203", "2026-01-05", 200.0],
    ]
    assert app.describe(ref, "jquants.market_calendar")["partition_grain"] == "year"
    assert all("/year=2025/" not in key for key in reads if key.endswith(".parquet"))
    assert result.structured_content["sources"][1]["coverage_status"] == "partial"


def test_empty_period_is_valid_and_does_not_fetch_parquet(adapter):
    app, reads, ref = adapter
    result = app.call(
        lambda: app.query(
            request(
                ref,
                sources=[
                    {
                        "dataset": "jquants.daily_bars",
                        "alias": "prices",
                        "from": "2027-01-01",
                        "to": "2027-01-31",
                    }
                ],
            )
        )
    )
    assert not result.is_error
    assert result.structured_content["rows"] == []
    assert not any(key.endswith(".parquet") for key in reads)


@pytest.mark.parametrize("change", ["digest", "bytes", "rows", "schema", "404"])
def test_corrupt_parquet_fails_closed(adapter, lake, change):
    app, _reads, ref = adapter
    release = app.fixed(ref)
    obj = release.dataset_manifest("jquants.daily_bars").partitions[0].objects[0]
    path = lake[0] / obj.key
    if change == "404":
        path.unlink()
    elif change in ("digest", "bytes"):
        raw = path.read_bytes()
        path.write_bytes(b"x" + raw[1:] if change == "digest" else raw + b"x")
    else:
        # The graph is valid; the physical object's contract or row count is not.
        if change == "rows":
            altered = obj.model_copy(update={"rows": obj.rows + 1})
            part = release.dataset_manifest("jquants.daily_bars").partitions[0]
            manifest = release.dataset_manifest("jquants.daily_bars").model_copy(
                update={
                    "partitions": (part.model_copy(update={"objects": (altered,)}),),
                }
            )
            release = replace(
                release,
                dataset_manifests={**release.dataset_manifests, "jquants.daily_bars": manifest},
            )
        else:
            from tools.l1_mcp import query as module

            original = module.accepted_dataset

            def bad_schema(fixed, name):
                ds = original(fixed, name)
                return replace(ds, columns=ds.columns[:-1])

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(module, "accepted_dataset", bad_schema)
                result = app.call(lambda: module.execute_query(release, request(ref), app.cache))
            assert result.structured_content["error"]["code"] == "INTEGRITY_ERROR"
            return
        app.fixed = lambda _: release
    result = app.call(lambda: app.query(request(ref)))
    assert result.is_error
    assert result.structured_content["error"]["code"] == (
        "RELEASE_UNAVAILABLE" if change == "404" else "INTEGRITY_ERROR"
    )
    assert "TEST-ONLY-SECRET" not in result.model_dump_json()


def test_manifest_digest_and_contract_mismatch(adapter):
    app, _, ref = adapter
    bad = ref.model_copy(update={"manifest_sha256": "0" * 64})
    result = app.call(lambda: app.describe(bad, "jquants.daily_bars"))
    assert result.structured_content["error"]["code"] == "INTEGRITY_ERROR"
    release = app.fixed(ref)
    manifests = dict(release.dataset_manifests)
    manifests["jquants.daily_bars"] = manifests["jquants.daily_bars"].model_copy(
        update={"contract_version": 99}
    )
    app.fixed = lambda _: replace(release, dataset_manifests=manifests)
    result = app.call(lambda: app.describe(ref, "jquants.daily_bars"))
    assert result.structured_content["error"]["code"] == "CONTRACT_MISMATCH"


def test_manifest_preflight_budget_before_download(adapter):
    app, reads, ref = adapter
    app.limits = replace(LIMITS, query_bytes=1)
    result = app.call(lambda: app.query(request(ref)))
    assert result.structured_content["error"]["code"] == "QUERY_LIMIT_EXCEEDED"
    assert not any(key.endswith(".parquet") for key in reads)


@pytest.mark.parametrize(
    "updates",
    [
        {"max_rows": 0},
        {"max_rows": 2001},
        {"max_rows": True},
        {"sql": "x" * 16385},
        {"parameters": {"n": float("nan")}},
        {"parameters": {"n": []}},
        {"sources": []},
        {"sources": [{"dataset": "x", "alias": "x", "from": "2026-02-30", "to": "2026-03-01"}]},
        {"sources": [{"dataset": "x", "alias": "X", "from": "2026-02-01", "to": "2026-03-01"}]},
        {"sources": [{"dataset": "x", "alias": "x", "from": "2026-03-01", "to": "2026-02-01"}]},
    ],
)
def test_invalid_inputs(updates):
    with pytest.raises(ValidationError):
        request(ReleaseRef(release_id="test", manifest_sha256="a" * 64), **updates)


def test_busy_and_output_envelope_budget(adapter):
    app, _, _ = adapter
    with app.lock:
        assert app.call(app.resolve).structured_content["error"]["code"] == "BUSY"
    app.limits = replace(LIMITS, result_bytes=1000)
    result = app.call(lambda: {"rows": ["x" * 950]})
    assert result.structured_content["error"]["code"] == "RESULT_TOO_LARGE"


def child(sql, tmp_path, *, limits=LIMITS, max_rows=20, parameters=None):
    return run_child(
        {
            "sources": [],
            "sql": sql,
            "parameters": parameters or {},
            "max_rows": max_rows,
            "limits": asdict(limits),
        },
        tmp_path,
        limits,
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "CREATE TABLE x(i INT)",
        "INSERT INTO x VALUES (1)",
        "COPY (SELECT 1) TO '/tmp/l1-mcp-should-not-exist'",
        "ATTACH '/tmp/private.sqlite' AS a",
        "PRAGMA version",
        "SET threads=2",
        "CALL pragma_version()",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_blob('/etc/passwd')",
        "SELECT * FROM read_parquet('https://example.com/data.parquet')",
        "SELECT * FROM glob('/tmp/*')",
        "SELECT * FROM query('COPY (SELECT 1) TO ''/tmp/unsafe''')",
        "SELECT * FROM query('SET enable_external_access=true')",
        "SELECT * FROM undeclared",
    ],
)
def test_rejected_sql(sql, tmp_path):
    with pytest.raises(L1Error, match="QUERY_REJECTED"):
        child(sql, tmp_path)


def test_result_rows_bytes_and_json_types(tmp_path):
    with pytest.raises(L1Error, match="RESULT_TOO_LARGE"):
        child("SELECT * FROM range(21)", tmp_path)
    with pytest.raises(L1Error, match="RESULT_TOO_LARGE"):
        child("SELECT repeat('x', 262144)", tmp_path)
    for sql in ("SELECT 'NaN'::DOUBLE", "SELECT 'Infinity'::DOUBLE", "SELECT [1,2]"):
        with pytest.raises(L1Error, match="UNSUPPORTED_RESULT_TYPE"):
            child(sql, tmp_path)
    result = child(
        "SELECT NULL, 0, '', 9007199254740993::BIGINT, 1.23::DECIMAL(4,2), DATE '2026-01-01', 'x'::BLOB",
        tmp_path,
    )
    assert result["rows"] == [[None, 0, "", 9007199254740993, "1.23", "2026-01-01", "eA=="]]
    assert child("SELECT $v", tmp_path, parameters={"v": "';DROP TABLE x;--"})["rows"] == [
        ["';DROP TABLE x;--"]
    ]


def test_timeout_reaps_child_and_secret_free_environment(tmp_path, monkeypatch):
    from tools.l1_mcp import query as module

    original = subprocess.Popen
    processes = []

    def capture(*args, **kwargs):
        assert set(kwargs["env"]) == {"LANG", "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"}
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "parent-only-secret")
    monkeypatch.setenv("READ_ACCESS_TOKEN", "parent-only-read-secret")
    monkeypatch.setattr(module.subprocess, "Popen", capture)
    with pytest.raises(L1Error, match="QUERY_TIMEOUT"):
        child(
            "WITH RECURSIVE x(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM x) SELECT sum(i) FROM x",
            tmp_path,
            limits=replace(LIMITS, sql_seconds=1),
        )
    assert processes
    assert processes[0].poll() is not None
    assert not list(tmp_path.glob("query-*"))


def test_mcp_discovery_call_and_error_shape(adapter):
    app, _, _ = adapter

    async def check():
        async with Client(create_server(app)) as client:
            listing = await client.list_tools()
            assert {tool.name for tool in listing.tools} == {
                "l1_resolve_current",
                "l1_describe_dataset",
                "l1_query",
            }
            for tool in listing.tools:
                assert tool.annotations.read_only_hint is True
                assert tool.annotations.destructive_hint is False
                assert tool.annotations.open_world_hint is False
            resolved = await client.call_tool("l1_resolve_current", {})
            assert not resolved.is_error
            ref = resolved.structured_content["release_ref"]
            result = await client.call_tool(
                "l1_query",
                {
                    "release_ref": ref,
                    "sources": [
                        {
                            "dataset": "jquants.daily_bars",
                            "alias": "p",
                            "from": "2026-01-01",
                            "to": "2026-01-31",
                        }
                    ],
                    "sql": "SELECT count(*) FROM p",
                },
            )
            assert result.structured_content["rows"] == [[3]]
            assert len(wire(result.model_dump(mode="json", by_alias=True))) < 262144
            invalid = await client.call_tool(
                "l1_query",
                {"release_ref": ref, "sources": [], "sql": "SELECT 1", "max_rows": True},
            )
            assert invalid.is_error

    asyncio.run(check())


@pytest.mark.parametrize("status", [302, 401, 403, 500])
def test_gateway_rejects_redirect_and_upstream_errors(tmp_path, status):
    gateway = Gateway("TEST-ONLY-SECRET", tmp_path)
    response = requests.Response()
    response.status_code = status
    response._content = b"private upstream detail"
    response._content_consumed = True
    response.headers["Location"] = "https://untrusted.invalid/"
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        assert kwargs["allow_redirects"] is False
        return response

    gateway.session.get = get
    app = Adapter(source=gateway)
    try:
        result = app.call(app.resolve)
        assert result.structured_content["error"]["code"] == "UPSTREAM_UNAVAILABLE"
        assert len(calls) == 1
        assert b"private" not in wire(result.model_dump(mode="json", by_alias=True))
        assert b"SECRET" not in wire(result.model_dump(mode="json", by_alias=True))
    finally:
        app.close()


@pytest.mark.parametrize("declared", [True, False])
def test_gateway_stream_and_content_length_budget(tmp_path, declared):
    gateway = Gateway("TEST-ONLY-SECRET", tmp_path, replace(LIMITS, process_bytes=4))
    response = requests.Response()
    response.status_code = 200
    response._content = b"12345"
    response._content_consumed = True
    if declared:
        response.headers["Content-Length"] = "5"
    gateway.session.get = lambda *args, **kwargs: response
    try:
        with pytest.raises(L1Error, match="TRANSFER_BUDGET_EXCEEDED"):
            gateway.read_bytes(current_l1_pointer_key())
        assert gateway.gets == 1
        assert gateway.transferred_bytes == (0 if declared else 5)
    finally:
        gateway.close()


def test_gateway_get_budget_and_invalid_namespace_make_no_request(tmp_path):
    gateway = Gateway("TEST-ONLY-SECRET", tmp_path, replace(LIMITS, process_gets=0))
    try:
        with pytest.raises(L1Error, match="TRANSFER_BUDGET_EXCEEDED"):
            gateway.read_bytes(current_l1_pointer_key())
        with pytest.raises((L1Error, ValueError)):
            gateway.read_bytes("../private")
        assert gateway.gets == 0
    finally:
        gateway.close()


def test_pointer_changes_during_object_fetch_do_not_change_release(adapter, lake):
    app, reads, ref = adapter
    gateway = app.source
    original = gateway.session.get
    pointer_path = lake[0] / current_l1_pointer_key()

    def change_pointer(url, **kwargs):
        if (kwargs.get("params") or {}).get("key", "").endswith(".parquet"):
            pointer_path.write_bytes(b'{"release_id":"changed"}')
        return original(url, **kwargs)

    gateway.session.get = change_pointer
    result = app.call(lambda: app.query(request(ref)))
    assert not result.is_error
    assert result.structured_content["release_ref"] == ref.model_dump()
    assert result.structured_content["row_count"] == 3
    assert current_l1_pointer_key() not in reads


def test_unknown_columns_fail_before_object_get(adapter):
    app, reads, ref = adapter
    inputs = request(ref).model_dump(by_alias=True)
    inputs["sources"][0]["columns"] = ["private"]
    result = app.call(lambda: app.query(Query.model_validate(inputs)))
    assert result.structured_content["error"]["code"] == "INVALID_ARGUMENT"
    assert not any(key.endswith(".parquet") for key in reads)


def test_memory_limit_does_not_return_partial_rows(tmp_path):
    with pytest.raises(L1Error, match="QUERY_LIMIT_EXCEEDED"):
        child(
            "SELECT count(DISTINCT i) FROM range(2000000) AS t(i)",
            tmp_path,
            limits=replace(LIMITS, duckdb_memory_bytes=8 * 1024 * 1024),
        )
