import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.acquisition.policy import acquisition_groups
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    confirm_source_response,
)
from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError, R2Error
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.acquisition.receipts import available_handoffs
from baibai_jobs.acquisition.source_inputs import input_key, load_input
from baibai_jobs.integration.economic_sources import integrate
from baibai_jobs.publication.lease import _save_handle, acquire, release

DAY = date(2020, 1, 1)
AT = datetime(2026, 11, 1, tzinfo=UTC)
SOURCE = "fred-payems"


@pytest.fixture
def setup(tmp_path):
    store = MemoryR2Store()
    path = tmp_path / "initial.sqlite"
    with closing(initialize_database(path)):
        pass
    store.store_object("indicators.sqlite", path.read_bytes())
    handle, _ = acquire(store, "daily")
    lease = tmp_path / "lease.json"
    _save_handle(lease, handle)
    return store, lease, handle


def stage(tmp_path, store, value="A", clock=AT, name="one", *, source=SOURCE, group="fred_csv"):
    directory = tmp_path / name
    directory.mkdir()
    with closing(initialize_database(directory / "receipts.sqlite")) as conn, conn:
        confirm_source_response(
            conn, source_id=source, fetched_at=clock, records_by_day={DAY: [{"value": value}]}
        )
    (directory / "result.json").write_text(
        json.dumps(
            {
                "group": group,
                "sources": [{"source_id": source, "status": "ok"}],
                "started_at": clock.isoformat(),
                "asof": DAY.isoformat(),
            }
        )
    )
    save_handoff(store, group + "-" + name, directory)


def run(tmp_path, setup, name="run"):
    store, lease, _ = setup
    return integrate(
        store, tmp_path / name, database=tmp_path / f"{name}.sqlite", asof=DAY, lease_handle=lease
    )


def test_l1_and_every_input_publish_without_market_runs_lake_or_models(tmp_path, setup):
    store, _, _ = setup
    stage(tmp_path, store)
    report = run(tmp_path, setup)
    assert report["l1_published"]
    assert report["l1_sealed"]
    assert report["inputs_updated"] == ["economic-" + group for group in acquisition_groups()]
    assert not available_handoffs(store, market=False)
    result = load_input(store, "economic-fred_csv", tmp_path / "input.sqlite", tmp_path / "read")
    assert result["canonical_generations"] == {
        "economic_etag": store.head("indicators.sqlite").etag
    }
    with closing(sqlite3.connect(tmp_path / "input.sqlite")) as conn:
        assert conn.execute("SELECT count(*) FROM economic_source_records").fetchone()[0] == 0
    with closing(sqlite3.connect(tmp_path / "run.sqlite")) as conn:
        assert next(read_snapshots(conn, DAY, DAY, source_id=SOURCE)).records == ({"value": "A"},)
    assert not any(
        key.startswith("lake/") or key in {"market.sqlite", "runs.sqlite", "machine-manifest.json"}
        for key in store.get_keys + store.head_keys + store.put_keys
    )
    # Later independent groups must not revert an already-sealed source.
    later = AT + timedelta(hours=1)
    stage(tmp_path, store, source="fred-vixcls", name="next-fred", value="B", clock=later)
    stage(
        tmp_path,
        store,
        group="mof_jgb",
        source="mof-jgbcm-all",
        name="next-mof",
        value="C",
        clock=later,
    )
    second = run(tmp_path, setup, "next")
    assert second["l1_sealed"]
    assert not any(row["failed"] for row in second["handoffs"])
    with closing(sqlite3.connect(tmp_path / "next.sqlite")) as conn:
        assert conn.execute("SELECT count(*) FROM economic_source_records").fetchone()[0] == 3
        for source, expected in ((SOURCE, "A"), ("fred-vixcls", "B"), ("mof-jgbcm-all", "C")):
            assert next(read_snapshots(conn, DAY, DAY, source_id=source)).records == (
                {"value": expected},
            )


def test_l1_put_unknown_ack_is_resolved_by_bytes(tmp_path, setup, monkeypatch):
    store, _, _ = setup
    stage(tmp_path, store)
    real = store.put_file

    def put(key, *args, **kwargs):
        result = real(key, *args, **kwargs)
        if key == "indicators.sqlite":
            raise R2Error("uncertain")
        return result

    monkeypatch.setattr(store, "put_file", put)
    assert run(tmp_path, setup)["l1_sealed"]


@pytest.mark.parametrize("point", ["readback", "seal", "inputs"])
def test_fresh_runner_recovers_after_publication_without_refetch_or_new_originals(
    tmp_path, setup, monkeypatch, point
):
    store, _, _ = setup
    stage(tmp_path, store)
    real_put, real_get = store.put_file, store.download_file
    uploaded = False

    def put(key, *args, **kwargs):
        nonlocal uploaded
        if point == "seal" and key.endswith("/integrated.json"):
            raise R2Error("stop before seal")
        if point == "inputs" and key.startswith("coordination/source-inputs/economic-fred_csv"):
            raise R2Error("stop midway inputs")
        result = real_put(key, *args, **kwargs)
        if key == "indicators.sqlite":
            uploaded = True
        return result

    def get(key, *args, **kwargs):
        if point == "readback" and uploaded and key == "indicators.sqlite":
            raise R2Error("readback unavailable")
        return real_get(key, *args, **kwargs)

    monkeypatch.setattr(store, "put_file", put)
    monkeypatch.setattr(store, "download_file", get)
    with pytest.raises(LakePublishError):
        run(tmp_path, setup)
    assert not any(key.endswith("/integrated.json") for key in store.values) or point == "inputs"
    monkeypatch.setattr(store, "put_file", real_put)
    monkeypatch.setattr(store, "download_file", real_get)
    result = run(tmp_path, setup, "retry")
    assert result["l1_sealed"]
    assert sorted(result["inputs_updated"] + result["inputs_reused"]) == sorted(
        "economic-" + group for group in acquisition_groups()
    )
    if point == "inputs":
        groups = list(acquisition_groups())
        assert result["inputs_reused"] == [
            "economic-" + group for group in groups[: groups.index("fred_csv")]
        ]
    with closing(sqlite3.connect(tmp_path / "retry.sqlite")) as conn:
        assert conn.execute("SELECT count(*) FROM economic_source_records").fetchone()[0] == 1


def test_wrong_group_source_isolated_from_valid_handoff(tmp_path, setup):
    store, _, _ = setup
    stage(tmp_path, store, group="boj", name="wrong")
    stage(tmp_path, store, name="right")
    report = run(tmp_path, setup)
    assert report["handoffs"][0]["failed"]
    assert not report["handoffs"][1]["failed"]
    assert report["l1_published"]


def test_handoff_validation_timer_counts_only_entered_checks_including_failure(
    tmp_path, setup, monkeypatch, capsys
):
    from baibai_jobs.integration import native_source_merge

    store, lease, _ = setup
    stage(tmp_path, store, group="boj", name="wrong")  # rejected before merge
    stage(tmp_path, store, name="first")
    stage(tmp_path, store, name="next", clock=AT + timedelta(hours=1))
    validate = native_source_merge.validate_collection_confirmations
    checks = []

    def fail_first(conn):
        validate(conn)
        checks.append(None)
        if len(checks) == 1:
            raise ValueError("private validation error")

    ticks = iter((1.0, 2.25, 4.0, 6.5))
    monkeypatch.setattr(native_source_merge, "validate_collection_confirmations", fail_first)
    monkeypatch.setattr(native_source_merge, "perf_counter", lambda: next(ticks))
    report = integrate(
        store,
        tmp_path / "plan",
        database=tmp_path / "out.sqlite",
        asof=DAY,
        lease_handle=lease,
        log_publication=True,
    )
    captured = capsys.readouterr()
    assert captured.err == (
        "economic L1 handoff: phase=validate_collection_confirmations "
        "checks=2 elapsed_seconds=3.750\n"
    )
    assert [r["applied"] for r in report["handoffs"]] == [False, False, True]
    assert report["handoffs"][1]["result"] == {"reason": "MergeError"}
    assert json.loads(captured.out)["handoffs"] == report["handoffs"]
    assert json.loads((tmp_path / "plan/applied.json").read_bytes()) == report


def test_expired_lease_before_seal_does_not_advance_watermark(tmp_path, setup, monkeypatch):
    store, _, handle = setup
    stage(tmp_path, store)
    original = store.download_file
    calls = 0

    def download(key, *args, **kwargs):
        nonlocal calls
        result = original(key, *args, **kwargs)
        if key == "indicators.sqlite":
            calls += 1
            if calls == 2:
                release(store, handle)
        return result

    monkeypatch.setattr(store, "download_file", download)
    with pytest.raises(RuntimeError):
        run(tmp_path, setup)
    assert not any(key.endswith("/integrated.json") for key in store.values)


def test_ordered_seal_failure_never_skips_forward_to_later_source_clock(
    tmp_path, setup, monkeypatch
):
    store, _, _ = setup
    stage(tmp_path, store, name="first")
    stage(tmp_path, store, value="B", clock=AT + timedelta(hours=1), name="second")
    real = store.put_file

    def put(key, *args, **kwargs):
        if key.endswith("fred_csv-first/integrated.json"):
            raise R2Error("first seal failed")
        return real(key, *args, **kwargs)

    monkeypatch.setattr(store, "put_file", put)
    with pytest.raises(R2Error):
        run(tmp_path, setup)
    assert not any(key.endswith("/integrated.json") for key in store.values)
    monkeypatch.setattr(store, "put_file", real)
    assert run(tmp_path, setup, "retry")["l1_sealed"]


def test_backup_is_exact_fresh_base_and_no_stage_does_not_republish(tmp_path, setup):
    store, _, _ = setup
    old = store.get_bytes("indicators.sqlite")
    stage(tmp_path, store)
    run(tmp_path, setup)
    assert store.get_bytes("indicators.sqlite.bak") == old
    before = len([key for key in store.put_keys if key == "indicators.sqlite"])
    downloads = store.get_keys.count("indicators.sqlite")
    original = store.head("indicators.sqlite")
    old_writes = list(store.put_keys)
    result = run(tmp_path, setup, "no-new-stage")
    assert store.put_keys == old_writes
    assert not list((tmp_path / "no-new-stage").glob("*/canonical.sqlite"))
    assert len([key for key in store.put_keys if key == "indicators.sqlite"]) == before
    assert store.get_keys.count("indicators.sqlite") - downloads == 1
    assert store.head("indicators.sqlite") == original
    assert result["canonical_generations"] == {"economic_etag": original.etag}
    assert result["inputs_updated"] == []
    assert result["inputs_reused"] == ["economic-" + group for group in acquisition_groups()]
    assert store.get_bytes("indicators.sqlite.bak") == old


def test_changed_planning_pointer_after_checked_get_stops_before_any_new_put(
    tmp_path, setup, monkeypatch
):
    store, lease, _ = setup
    run(tmp_path, setup)
    first = input_key("economic-" + acquisition_groups()[0])
    before = list(store.put_keys)

    def checked(key, *, max_bytes):
        result = store.get_bytes_with_metadata(key)
        if key == first:
            store.store_object(key, b'{"stage_id":"replaced"}')
        return result

    monkeypatch.setattr(store, "get_checked_object", checked, raising=False)
    with pytest.raises(LakePublishError, match="private JSON object changed"):
        integrate(
            store,
            tmp_path / "next",
            database=tmp_path / "next.sqlite",
            asof=DAY + timedelta(days=1),
            lease_handle=lease,
        )
    assert store.put_keys == before


@pytest.mark.parametrize("guard", [3, 4], ids=["before-publication", "before-source-state"])
@pytest.mark.parametrize("missing", [False, True], ids=["changed", "missing"])
def test_unchanged_canonical_still_checks_remote_generation(
    tmp_path, setup, monkeypatch, guard, missing
):
    store, _, _ = setup
    replacement = tmp_path / "replacement.sqlite"
    with closing(initialize_database(replacement)) as conn, conn:
        confirm_source_response(
            conn, source_id=SOURCE, fetched_at=AT, records_by_day={DAY: [{"value": "B"}]}
        )
    real = store.head
    put_keys = list(store.put_keys)
    reads = 0

    def head(key):
        nonlocal reads
        if key == "indicators.sqlite":
            reads += 1
            if reads == guard:
                if missing:
                    del store.values[key]
                else:
                    store.store_object(key, replacement.read_bytes())
        return real(key)

    monkeypatch.setattr(store, "head", head)
    with pytest.raises(LakeCASConflict, match="economic canonical changed before"):
        run(tmp_path, setup)
    assert store.get_keys.count("indicators.sqlite") == 1
    assert store.put_keys == put_keys
    assert not (tmp_path / "run.sqlite").exists()


def test_unchanged_canonical_does_not_reuse_read_after_lease_expiry(tmp_path, setup, monkeypatch):
    store, _, handle = setup
    real = store.download_file

    def download(key, *args, **kwargs):
        result = real(key, *args, **kwargs)
        if key == "indicators.sqlite":
            release(store, handle)
        return result

    monkeypatch.setattr(store, "download_file", download)
    with pytest.raises(RuntimeError):
        run(tmp_path, setup)
    assert not any(key.startswith("coordination/source-inputs/") for key in store.put_keys)
    assert not (tmp_path / "run.sqlite").exists()


def test_backup_failure_does_not_replace_canonical_or_seal(tmp_path, setup, monkeypatch):
    store, _, _ = setup
    old = store.get_bytes("indicators.sqlite")
    stage(tmp_path, store)
    real = store.put_file

    def put(key, *args, **kwargs):
        if key == "indicators.sqlite.bak":
            raise R2Error("backup unavailable")
        return real(key, *args, **kwargs)

    monkeypatch.setattr(store, "put_file", put)
    with pytest.raises((ValueError, LakePublishError)):
        run(tmp_path, setup)
    assert store.get_bytes("indicators.sqlite") == old
    assert "indicators.sqlite" not in store.put_keys
    assert not any(key.endswith("/integrated.json") for key in store.values)


def test_tradingview_explicit_native_capture_remains_independent(tmp_path, setup):
    from tests.helpers.tradingview_market import capture

    from baibai_jobs.acquisition.market_context import prepare, read_exact

    store, _, _ = setup
    raw = capture(receipt_id="mcp-one")
    path = tmp_path / "capture.json"
    path.write_text(raw.model_dump_json(by_alias=True))
    directory = tmp_path / "capture"
    prepare([path], directory)
    save_handoff(store, "tradingview_market-one", directory)
    report = run(tmp_path, setup)
    assert report["handoffs"][0]["applied"]
    assert not report["handoffs"][0]["failed"]
    assert (
        read_exact(tmp_path / "run.sqlite", raw.receipt_id, raw.digest())["observation"][
            "capture_sha256"
        ]
        == raw.digest()
    )


def test_market_owner_rejects_economic_plan_before_touching_any_database(tmp_path):
    from baibai_jobs.integration.sources import apply

    (tmp_path / "plan.json").write_text(json.dumps({"handoffs": [{"market": False}]}))
    with pytest.raises(ValueError, match="independent L1"):
        apply(object(), tmp_path, market=tmp_path / "absent.sqlite", asof=DAY)
    assert not (tmp_path / "absent.sqlite").exists()


def test_l1_readback_remains_visible_when_following_seal_fails(tmp_path, setup, monkeypatch):
    store, _, _ = setup
    stage(tmp_path, store)
    real = store.put_file

    def put(key, *args, **kwargs):
        if key.endswith("/integrated.json"):
            raise R2Error("seal failed")
        return real(key, *args, **kwargs)

    monkeypatch.setattr(store, "put_file", put)
    with pytest.raises(R2Error):
        run(tmp_path, setup)
    report = json.loads((tmp_path / "run/applied.json").read_text())
    assert report["l1_published"]
    assert not report.get("l1_sealed")
    from baibai_jobs.observability.discord import combined_integration_report

    merged = combined_integration_report({}, report)
    assert merged["economic_l1_published"]


@pytest.mark.parametrize("diagnostics", [{}, {"error_types": ["SourceWithheldError", "HTTPError"]}])
def test_retained_acquisition_failure_is_a_gap_not_failed_publication(
    tmp_path, setup, capsys, diagnostics
):
    from baibai_engine.data_collection.receipts.native_sources.store import record_source_run
    from baibai_jobs.integration.sources import main as check
    from baibai_jobs.observability.discord import render_source_failures

    store, _, _ = setup
    directory = tmp_path / "failure"
    directory.mkdir()
    with closing(initialize_database(directory / "receipts.sqlite")) as conn, conn:
        record_source_run(
            conn,
            source_id=SOURCE,
            start=DAY,
            end=DAY,
            started_at=AT,
            status="failed",
            record_count=0,
            error_message="private provider failure",
        )
    (directory / "result.json").write_text(
        json.dumps(
            {
                "group": "fred_csv",
                "started_at": AT.isoformat(),
                "asof": DAY.isoformat(),
                "sources": [
                    {
                        "source_id": SOURCE,
                        "status": "failed",
                        "http_status": 403,
                        **diagnostics,
                        "history_recovery_required": True,
                    }
                ],
            }
        )
    )
    save_handoff(store, "fred_csv-failure", directory)
    report = run(tmp_path, setup)
    assert report["l1_published"]
    assert report["l1_sealed"]
    assert not report["handoffs"][0]["failed"]
    result = report["handoffs"][0]["result"]
    assert result["acquisition_failures"] == [
        {"source_id": SOURCE, "status": "failed", "http_status": 403, **diagnostics}
    ]
    assert result["acquisition_issues"] == [SOURCE + ":history_recovery_required"]
    assert check(["check", "--directory", str(tmp_path / "run")]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["integration_status"] == "completed_with_source_gaps"
    assert "受領1" in render_source_failures(report)[0]
    assert "private provider failure" not in json.dumps(report)


@pytest.mark.parametrize(
    ("navigation", "expected"),
    [
        ("html", "navigation returned HTTP 200 (content-type 'text/html')"),
        ("error", "navigation failed: PlaywrightError (net::ERR_CONNECTION_RESET)"),
        ("none", "navigation returned no response"),
    ],
)
def test_historical_browser_download_failure_keeps_only_safe_diagnostic_through_l1(
    tmp_path, setup, monkeypatch, capsys, navigation, expected
):
    from unittest.mock import MagicMock

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    import baibai_engine.data_collection.acquisition.refresh as acquisition
    from baibai_engine.data_collection.transport.browser import BrowserFetcher
    from baibai_engine.data_collection.transport.context import (
        BrowserDownloadTimeoutError,
        IndicatorsProviderError,
    )
    from baibai_jobs.integration.economic_handoff import acquire as acquire_source

    secret = "SYNTHETIC_SECRET_MARKER"
    private_url = f"https://redirect.example.test/{secret}?token={secret}"

    # Replay the former native provider at the acquisition seam, not in today's
    # H.15 transport. Use the real navigation/error/diagnostic path below.
    def legacy_download(_source, *, start, end, context):
        try:
            context.browser_fetcher().fetch_download(private_url, max_bytes=1000)
        except BrowserDownloadTimeoutError as error:
            raise IndicatorsProviderError(f"private response {secret}") from error
        pytest.fail("expected a synthetic browser timeout")

    native = MagicMock(side_effect=legacy_download)
    monkeypatch.setattr(acquisition, "fetch_native_records", native)
    context = MagicMock()
    page = context.new_page.return_value
    response = page.goto.return_value
    response.status = 200
    response.headers = {"content-type": f"text/html; source={private_url}"}
    if navigation == "error":
        page.goto.side_effect = PlaywrightError(
            f"Page.goto: net::ERR_CONNECTION_RESET at {private_url}"
        )
    elif navigation == "none":
        page.goto.return_value = None
    page.expect_download.return_value.__exit__.side_effect = PlaywrightTimeoutError(private_url)
    monkeypatch.setattr(BrowserFetcher, "_ensure_context", lambda self: context)
    database = tmp_path / "source.sqlite"
    with closing(initialize_database(database)) as conn, conn:
        confirm_source_response(
            conn, source_id="fed-h15", fetched_at=AT, records_by_day={DAY: [{"value": "retained"}]}
        )
    directory = tmp_path / "browser-failure"
    assert acquire_source("frb_h15", "", DAY, directory, database) == 1
    source_report = json.loads((directory / "result.json").read_text())
    diagnostic = "no download started within 60000 ms; " + expected
    failure = {
        "source_id": "fed-h15",
        "status": "failed",
        "error_types": ["IndicatorsProviderError", "BrowserDownloadTimeoutError", "TimeoutError"],
        "browser_download_diagnostic": diagnostic,
    }
    assert source_report["sources"] == [{**failure, "history_recovery_required": False}]
    native.assert_called_once()
    page.goto.assert_called_once()
    page.expect_download.assert_called_once_with(timeout=60000)
    page.close.assert_called_once_with()
    response.body.assert_not_called()
    with closing(sqlite3.connect(database)) as conn:
        assert next(read_snapshots(conn, DAY, DAY, source_id="fed-h15")).records == (
            {"value": "retained"},
        )
    store, _, _ = setup
    save_handoff(store, "frb_h15-browser-failure", directory)
    report = run(tmp_path, setup)
    assert report["l1_published"]
    assert report["l1_sealed"]
    assert not report["handoffs"][0]["failed"]
    assert report["handoffs"][0]["result"]["acquisition_failures"] == [failure]
    public = json.dumps(report) + json.dumps(source_report) + capsys.readouterr().out
    assert secret not in public
    assert "redirect.example.test" not in public
    assert "private response" not in public


def test_pmi_group_stop_survives_private_handoff_and_l1_integration(tmp_path, setup):
    from baibai_engine.data_collection.receipts.native_sources.store import record_source_run
    from baibai_jobs.observability.discord import render_source_failures

    store, _, _ = setup
    directory = tmp_path / "pmi-stop"
    directory.mkdir()
    first, sibling = "spglobal-jp-pmi-mfg", "spglobal-jp-pmi-svc"
    with closing(initialize_database(directory / "receipts.sqlite")) as conn, conn:
        record_source_run(
            conn,
            source_id=first,
            start=DAY,
            end=DAY,
            started_at=AT,
            status="failed",
            record_count=0,
            error_message="private HTTP response",
        )
    failures = [
        {
            "source_id": first,
            "status": "failed",
            "http_status": 403,
            "error_types": ["ProviderFetchStoppedError", "SourceWithheldError", "HTTPError"],
        },
        {"source_id": sibling, "status": "failed", "blocked_by_source_id": first},
    ]
    (directory / "result.json").write_text(
        json.dumps(
            {
                "group": "spglobal_pmi",
                "started_at": AT.isoformat(),
                "asof": DAY.isoformat(),
                "sources": failures,
            }
        )
    )
    before = (directory / "receipts.sqlite").read_bytes()
    save_handoff(store, "pmi-stop", directory)
    report = run(tmp_path, setup)
    assert report["l1_published"]
    assert report["l1_sealed"]
    assert report["handoffs"][0]["result"]["acquisition_failures"] == failures
    assert not report["handoffs"][0]["failed"]
    assert not render_source_failures(report)
    assert (directory / "receipts.sqlite").read_bytes() == before
    with closing(sqlite3.connect(tmp_path / "run.sqlite")) as conn:
        assert conn.execute("SELECT source_id, status FROM economic_source_runs").fetchall() == [
            (first, "failed")
        ]


@pytest.mark.parametrize(
    ("group", "source"), [("fred_csv", SOURCE), ("boj", "boj-bank-lending-yoy")]
)
def test_unsealed_recheck_uses_complete_response_proof_and_next_acquisition_advances(
    tmp_path, setup, monkeypatch, capsys, group, source
):
    from baibai_engine.data_collection.receipts.native_sources.handoff import export_receipts
    from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
    from baibai_jobs.integration.sources import main as check
    from baibai_jobs.observability.discord import render_source_failures

    store, _, _ = setup

    def received(path, value, clock):
        with closing(initialize_database(path)) as conn, conn:
            if group == "fred_csv":
                confirm_source_response(
                    conn,
                    source_id=source,
                    fetched_at=clock,
                    records_by_day={DAY: [{"value": value}]},
                )
            else:
                store_snapshot(
                    conn,
                    source_id=source,
                    observed_at=DAY,
                    fetched_at=clock,
                    records=[{"value": value}],
                )

    def publish(packet, name, clock):
        (packet / "result.json").write_text(
            json.dumps(
                {
                    "group": group,
                    "sources": [{"source_id": source, "status": "ok"}],
                    "started_at": clock.isoformat(),
                    "asof": DAY.isoformat(),
                }
            )
        )
        save_handoff(store, group + "-" + name, packet)

    for index, value in enumerate("AAB"):
        packet = tmp_path / f"stage-{index}"
        packet.mkdir()
        clock = AT + timedelta(hours=index)
        received(packet / "receipts.sqlite", value, clock)
        publish(packet, str(index), clock)
    immutable = {
        key: value.body
        for key, value in store.values.items()
        if key.startswith("coordination/economic-sources/")
    }
    real = store.put_file

    def stop(key, *args, **kwargs):
        if key.endswith("/integrated.json"):
            raise R2Error("stop after confirmed canonical before seal")
        return real(key, *args, **kwargs)

    monkeypatch.setattr(store, "put_file", stop)
    with pytest.raises(R2Error):
        run(tmp_path, setup)
    initial = json.loads((tmp_path / "run/applied.json").read_text())
    assert initial["l1_published"]
    assert not any(item["failed"] for item in initial["handoffs"])
    monkeypatch.setattr(store, "put_file", real)
    retried = run(tmp_path, setup, "retry")
    failed = [item for item in retried["handoffs"] if item["failed"]]
    # FRED proves the old A recheck against A before the later B. A generic
    # scoped receipt lacks that complete-response proof and remains recoverable.
    recovery = group != "fred_csv"
    assert [item["entry"]["stage_id"] for item in failed] == ([group + "-1"] if recovery else [])
    if recovery:
        assert failed[0]["result"]["late_recovery_required"]
    assert check(["check", "--directory", str(tmp_path / "retry")]) == int(recovery)
    assert json.loads(capsys.readouterr().out)["integration_status"] == (
        "failed" if recovery else "completed"
    )
    assert bool(render_source_failures(retried)) is recovery
    marker = "coordination/economic-sources/" + group + "-1/integrated.json"
    marker_status = "needs_explicit_recovery" if recovery else "integrated"
    assert json.loads(store.get_bytes(marker))["status"] == marker_status
    assert retried["l1_published"]
    assert retried["l1_sealed"]
    assert len(retried["inputs_updated"]) == 15
    for key, body in immutable.items():
        assert store.get_bytes(key) == body

    # Both acquisition inputs omit old bodies; BOJ retains the exact day and
    # confirmation metadata while canonical keeps every adopted original.
    input_db = tmp_path / "source-input.sqlite"
    metadata = load_input(store, "economic-" + group, input_db, tmp_path / "input")
    assert metadata["canonical_generations"] == {
        "economic_etag": store.head("indicators.sqlite").etag
    }
    input_confirmation = None
    with closing(sqlite3.connect(input_db)) as conn:
        assert conn.execute("SELECT count(*) FROM economic_source_records").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM economic_source_documents").fetchone()[0] == 0
        if recovery:
            assert conn.execute(
                "SELECT observed_at FROM economic_boj_bank_lending_yoy_input_days"
            ).fetchall() == [(DAY.isoformat(),)]
            input_confirmation = conn.execute(
                "SELECT * FROM economic_source_record_confirmations WHERE source_id=?",
                (source,),
            ).fetchone()
            assert input_confirmation is not None
            assert datetime.fromisoformat(input_confirmation[2]) == AT + timedelta(hours=2)
            assert datetime.fromisoformat(input_confirmation[3]) == AT + timedelta(hours=2)
    with closing(sqlite3.connect(tmp_path / "retry.sqlite")) as conn:
        assert next(read_snapshots(conn, DAY, DAY, source_id=source)).records == ({"value": "B"},)
        assert conn.execute("SELECT count(*) FROM economic_source_records").fetchone()[0] == 2
        if recovery:
            assert (
                conn.execute(
                    "SELECT * FROM economic_source_record_confirmations WHERE source_id=?",
                    (source,),
                ).fetchone()
                == input_confirmation
            )
    packet = tmp_path / "next-stage"
    packet.mkdir()
    clock = AT + timedelta(hours=3)
    received(input_db, "C", clock)
    export_receipts(input_db, packet / "receipts.sqlite", source_ids=[source], since=clock)
    publish(packet, "next", clock)
    next_report = run(tmp_path, setup, "next")
    assert len(next_report["handoffs"]) == 1
    assert not next_report["handoffs"][0]["failed"]
    assert next_report["l1_sealed"]
    assert len(next_report["inputs_updated"]) == 15
    with closing(sqlite3.connect(tmp_path / "next.sqlite")) as conn:
        assert next(read_snapshots(conn, DAY, DAY, source_id=source)).records == ({"value": "C"},)
        assert conn.execute("SELECT count(*) FROM economic_source_records").fetchone()[0] == 3
    assert json.loads(store.get_bytes(marker))["status"] == marker_status


@pytest.mark.parametrize("initial_read_fails", [False, True])
def test_cli_logs_only_this_attempts_confirmed_publication(
    tmp_path, setup, monkeypatch, capsys, initial_read_fails
):
    from baibai_jobs.integration import economic_sources

    store, lease, _ = setup
    stage(tmp_path, store)
    directory = tmp_path / "cli"
    directory.mkdir()
    (directory / "applied.json").write_text(json.dumps({"l1_published": True, "asof": "old"}))
    monkeypatch.setattr(economic_sources, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    real_put, real_download = store.put_file, store.download_file

    def put(key, *args, **kwargs):
        if key.endswith("/integrated.json"):
            raise R2Error("private failure detail")
        return real_put(key, *args, **kwargs)

    def download(key, *args, **kwargs):
        if initial_read_fails:
            raise R2Error("private failure detail")
        return real_download(key, *args, **kwargs)

    monkeypatch.setattr(store, "put_file", put)
    monkeypatch.setattr(store, "download_file", download)
    assert (
        economic_sources.main(
            [
                "--directory",
                str(directory),
                "--db",
                str(tmp_path / "cli.sqlite"),
                "--asof",
                DAY.isoformat(),
                "--lease-handle",
                str(lease),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "private failure detail" not in captured.out + captured.err
    if initial_read_fails:
        assert captured.out == ""
    else:
        report = json.loads(captured.out)
        assert report["asof"] == DAY.isoformat()
        assert report["l1_published"]
        assert report["canonical_generations"] == {
            "economic_etag": store.head("indicators.sqlite").etag
        }
        assert report["handoffs"][0]["entry"]["stage_id"] == "fred_csv-one"
        assert len(report["handoffs"][0]["entry"]["manifest_sha256"]) == 64
        assert report["handoffs"][0]["result"]["late_recovery_required"] == []
