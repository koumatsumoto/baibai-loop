import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from botocore.exceptions import ReadTimeoutError
from tests.helpers.native_handoff import save_test_handoff
from tests.helpers.r2_store import VerifiedMemoryR2Store
from tests.helpers.tradingview_market import capture

from baibai_engine.data_collection.acquisition.policy import acquisition_groups
from baibai_engine.data_collection.providers.tradingview.market_models import MARKET_GROUP
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    confirm_source_response,
)
from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.storage.objects.r2 import (
    LakeCASConflict,
    LakePublishError,
    R2Error,
    R2OperationStopped,
)
from baibai_jobs.acquisition.economic_staging import FRED_HANDOFF_NAMESPACE, save_handoff
from baibai_jobs.acquisition.market_context import prepare as prepare_market_captures
from baibai_jobs.acquisition.market_context import read_exact as read_market_capture
from baibai_jobs.acquisition.receipts import available_handoffs
from baibai_jobs.acquisition.source_inputs import input_key, load_input
from baibai_jobs.integration.economic_sources import integrate
from baibai_jobs.publication.lease import LeaseError, _save_handle, acquire, release

DAY = date(2020, 1, 1)
AT = datetime(2026, 11, 1, tzinfo=UTC)
SOURCE = "fred-payems"


@pytest.fixture
def setup(tmp_path):
    store = VerifiedMemoryR2Store()
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
    save_test_handoff(store, group + "-" + name, directory, group)


def run(tmp_path, setup, name="run", *, asof=DAY):
    store, lease, _ = setup
    return integrate(
        store, tmp_path / name, database=tmp_path / f"{name}.sqlite", asof=asof, lease_handle=lease
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


@pytest.mark.parametrize("restore", [False, True])
def test_l1_initial_read_cannot_accept_another_same_size_generation(
    tmp_path, setup, monkeypatch, restore
):
    store, _, _ = setup
    bodies = []
    for value in ("A", "B"):
        path = tmp_path / f"seed-{value}.sqlite"
        with closing(initialize_database(path)) as connection, connection:
            confirm_source_response(
                connection,
                source_id=SOURCE,
                fetched_at=AT,
                records_by_day={DAY: [{"value": value}]},
            )
        bodies.append(path.read_bytes())
    first, second = bodies
    assert first != second
    assert len(first) == len(second)
    store.store_object("indicators.sqlite", first)
    download = store.download_file

    def replace_at_get(key, *args, **kwargs):
        if key != "indicators.sqlite":
            return download(key, *args, **kwargs)
        store.store_object(key, second)
        try:
            return download(key, *args, **kwargs)
        finally:
            if restore:
                store.store_object(key, first)

    with monkeypatch.context() as race:
        race.setattr(store, "download_file", replace_at_get)
        before = list(store.put_keys)
        with pytest.raises(LakeCASConflict):
            run(tmp_path, setup)
        assert store.put_keys == before
        assert not (tmp_path / "run.sqlite").exists()

    report = run(tmp_path, setup, "retry")
    assert report["canonical_generations"] == {
        "economic_etag": store.head("indicators.sqlite").etag
    }
    with closing(sqlite3.connect(tmp_path / "retry.sqlite")) as conn:
        assert next(read_snapshots(conn, DAY, DAY, source_id=SOURCE)).records == (
            {"value": "A" if restore else "B"},
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


def test_cli_releases_merge_copy_after_candidate_validation_before_upload(
    tmp_path, setup, monkeypatch, capsys
):
    from baibai_engine.storage.sqlite import snapshot
    from baibai_jobs.integration import economic_sources

    store, lease, _ = setup
    stage(tmp_path, store)
    before = store.values.copy()
    monkeypatch.setattr(economic_sources, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(store, "close", lambda: None, raising=False)
    validate = snapshot.validate_snapshot

    def reject_candidate(path):
        validate(path)
        raise sqlite3.DatabaseError("candidate validation failed")

    def invoke(name):
        return economic_sources.main(
            [
                "--directory",
                str(tmp_path / name),
                "--db",
                str(tmp_path / f"{name}.sqlite"),
                "--asof",
                DAY.isoformat(),
                "--lease-handle",
                str(lease),
            ]
        )

    with monkeypatch.context() as local:
        local.setattr(snapshot, "validate_snapshot", reject_candidate)
        assert invoke("failed") == 1
    assert capsys.readouterr().out == ""
    assert store.values == before
    assert len(available_handoffs(store, market=False)) == 1
    assert len(list((tmp_path / "failed").glob("*/canonical.sqlite"))) == 1
    assert not (tmp_path / "failed.sqlite").exists()

    put = store.put_file

    def assert_released(key, path, **kwargs):
        if key == "indicators.sqlite":
            assert not path.with_name("canonical.sqlite").exists()
            assert path.with_name("base.sqlite").read_bytes() == before[key].body
        return put(key, path, **kwargs)

    monkeypatch.setattr(store, "put_file", assert_released)
    assert invoke("retry") == 0
    assert not available_handoffs(store, market=False)
    with closing(sqlite3.connect(tmp_path / "retry.sqlite")) as conn:
        assert next(read_snapshots(conn, DAY, DAY, source_id=SOURCE)).records == ({"value": "A"},)


@pytest.mark.parametrize("point", ["readback", "seal", "inputs"])
def test_fresh_runner_recovers_after_publication_without_refetch_or_new_originals(
    tmp_path, setup, monkeypatch, point
):
    store, _, _ = setup
    stage(tmp_path, store)
    original = store.head("indicators.sqlite")
    old_bytes = store.get_bytes("indicators.sqlite")
    store.store_object("indicators.sqlite.bak", old_bytes)
    legacy = store.values["indicators.sqlite.bak"]
    real_put, real_get = store.put_file, store.download_file
    uploaded = False

    def put(key, *args, **kwargs):
        nonlocal uploaded
        if point == "seal" and key.endswith("/integrated.json"):
            raise R2Error("stop before seal")
        if point == "inputs" and key.startswith("coordination/source-inputs/economic-fred_csv"):
            raise R2Error("stop midway inputs")
        if key == "indicators.sqlite":
            assert kwargs["if_match"] == original.etag
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
    assert uploaded
    published = store.head("indicators.sqlite")
    assert published.etag != original.etag
    new_bytes = store.get_bytes("indicators.sqlite")
    assert new_bytes != old_bytes
    if point != "inputs":
        assert not any(key.endswith("/integrated.json") for key in store.values)
        assert len(available_handoffs(store, market=False)) == 1
    monkeypatch.setattr(store, "put_file", real_put)
    monkeypatch.setattr(store, "download_file", real_get)
    result = run(tmp_path, setup, "retry")
    assert result["l1_sealed"]
    assert result["canonical_generations"] == {"economic_etag": published.etag}
    assert store.get_bytes("indicators.sqlite") == new_bytes
    assert store.put_keys.count("indicators.sqlite") == 1
    assert store.values["indicators.sqlite.bak"] is legacy
    assert "indicators.sqlite.bak" not in store.head_keys + store.get_keys + store.put_keys
    assert not available_handoffs(store, market=False)
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
        assert next(read_snapshots(conn, DAY, DAY, source_id=SOURCE)).records == ({"value": "A"},)


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


def test_current_is_the_only_database_published_and_no_stage_does_not_republish(tmp_path, setup):
    store, _, _ = setup
    stage(tmp_path, store)
    run(tmp_path, setup)
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
    assert "indicators.sqlite.bak" not in store.values
    assert "indicators.sqlite.bak" not in store.head_keys + store.get_keys + store.put_keys


@pytest.mark.parametrize("next_day", [False, True])
def test_market_only_update_preserves_the_same_fifteen_planning_inputs(
    tmp_path, setup, next_day, monkeypatch
):
    from baibai_jobs.integration import economic_handoff

    store, _, _ = setup
    stage(tmp_path, store, name="old-boj", source="boj-bank-lending-yoy", group="boj")
    initial = run(tmp_path, setup, "base")
    prefix = "coordination/source-inputs/"
    original_inputs = {key: value for key, value in store.values.items() if key.startswith(prefix)}
    first_put = len(store.put_keys)
    first_delete = len(store.delete_keys)
    inputs = []
    captures = []
    for index in range(3):
        response = capture(
            receipt_id=f"market-{index}", bars=True, received=AT + timedelta(minutes=index)
        )
        path = tmp_path / f"capture-{index}.json"
        path.write_text(response.model_dump_json(by_alias=True))
        inputs.append(path)
        captures.append(response)
    directory = tmp_path / "market-packet"
    prepare_market_captures(inputs, directory, expected_captures=3)
    save_handoff(store, MARKET_GROUP + "-new", directory)
    asof = DAY + timedelta(days=1) if next_day else DAY
    report = run(tmp_path, setup, "market-only", asof=asof)
    assert report["l1_sealed"]
    assert report["canonical_generations"] != initial["canonical_generations"]
    assert report["inputs_updated"] == []
    assert report["inputs_reused"] == initial["inputs_updated"]
    assert "inputs_retirement_pending" not in report
    assert not any(key.startswith(prefix) for key in store.put_keys[first_put:])
    assert store.delete_keys[first_delete:] == []
    assert {key: value for key, value in store.values.items() if key.startswith(prefix)} == (
        original_inputs
    )
    for response in captures:
        assert (
            read_market_capture(
                tmp_path / "market-only.sqlite", response.receipt_id, response.digest()
            )["observation"]["capture_sha256"]
            == response.digest()
        )

    # The same acquisition entrypoint used by Actions accepts the old advisory date.
    monkeypatch.setattr(economic_handoff, "Boto3R2Store", lambda **_: store)
    seed = tmp_path / "seed.sqlite"
    load = [
        "input",
        "--group",
        "boj",
        "--db",
        str(seed),
        "--directory",
        str(tmp_path / "download"),
    ]
    assert economic_handoff.main(load) == 0
    metadata = json.loads((tmp_path / "download/result.json").read_text())
    assert metadata["asof"] == DAY.isoformat()
    assert metadata["canonical_generations"] == initial["canonical_generations"]
    seed.unlink()
    pointer = json.loads(store.values[input_key("economic-boj")].body)
    manifest = json.loads(store.values[prefix + pointer["stage_id"] + "/handoff.json"].body)
    del store.values[manifest["planning_key"]]
    assert economic_handoff.main(load) == 1
    assert not seed.exists()


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


def test_current_put_conflict_keeps_the_winning_database_and_unsealed_handoff(
    tmp_path, setup, monkeypatch
):
    store, _, _ = setup
    original = store.head("indicators.sqlite")
    stage(tmp_path, store)
    winner = tmp_path / "winner.sqlite"
    with closing(initialize_database(winner)) as conn, conn:
        confirm_source_response(
            conn, source_id=SOURCE, fetched_at=AT, records_by_day={DAY: [{"value": "B"}]}
        )
    winning_bytes = winner.read_bytes()
    real = store.put_file

    def put(key, *args, **kwargs):
        if key == "indicators.sqlite":
            assert kwargs["if_match"] == original.etag
            # The current version changes after the publisher's last HEAD.
            store.store_object(key, winning_bytes)
        return real(key, *args, **kwargs)

    monkeypatch.setattr(store, "put_file", put)
    with pytest.raises(LakePublishError, match="economic PUT is not confirmed"):
        run(tmp_path, setup)
    assert store.get_bytes("indicators.sqlite") == winning_bytes
    assert store.put_keys.count("indicators.sqlite") == 1
    assert "indicators.sqlite.bak" not in store.head_keys + store.get_keys + store.put_keys
    assert not any(key.endswith("/integrated.json") for key in store.values)
    assert len(available_handoffs(store, market=False)) == 1


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
    save_test_handoff(store, "fred_csv-failure", directory, "fred_csv")
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


@pytest.mark.parametrize("source", ["multpl-shiller-pe", "nikkei-per"])
def test_incomplete_valuation_markup_is_a_gap_not_an_empty_revision(
    tmp_path, setup, monkeypatch, capsys, source
):
    from baibai_engine.data_collection.providers import multpl
    from baibai_engine.data_collection.providers.nikkei import indices
    from baibai_jobs.integration.economic_handoff import acquire as acquire_source
    from baibai_jobs.integration.sources import main as check

    asof, retained_day = date(2026, 10, 15), date(2026, 10, 1)
    if source == "multpl-shiller-pe":
        provider, group, field, missing = multpl, "multpl", "value_text", "null"
        prefix = '<table id="datatable"><tr><td>Oct 15, 2026</td><td>41.1</td></tr>'
        earlier = "<tr><td>Oct 1, 2026</td><td>{value}</td></tr></table>"
        truncated = prefix + "<tr><td>Oct 1, 2026</td><td>"
    else:
        provider, group, field, missing = indices, "nikkei_indexes", "weighted_text", "-"
        # Nikkei can return a fragment containing completed rows, with no outer table.
        prefix = "<tr><td>2026.10.15</td><td>41.1</td><td>46.9</td></tr>"
        earlier = "<tr><!--daily_changing--><td>2026.10.01</td><td>{value}</td><td>21</td></tr>"
        truncated = prefix + earlier.split("{value}")[0]
    responses = iter(
        [prefix + earlier.format(value="20.0"), truncated, prefix + earlier.format(value=missing)]
    )
    monkeypatch.setattr(provider, "fetch_text", lambda *a, **kw: next(responses))
    store, _, _ = setup
    database = tmp_path / "source.sqlite"
    with closing(initialize_database(database)):
        pass

    def stage_acquisition(label, expected_exit):
        packet = tmp_path / (label + "-packet")
        assert acquire_source(group, source, asof, packet, database) == expected_exit
        save_handoff(store, group + "-" + label, packet)
        report = run(tmp_path, setup, label, asof=asof)
        assert report["l1_published"]
        assert report["l1_sealed"]
        assert not available_handoffs(store, market=False)
        assert not report["handoffs"][0]["failed"]
        with closing(sqlite3.connect(tmp_path / (label + ".sqlite"))) as conn:
            row = next(read_snapshots(conn, retained_day, retained_day, source_id=source))
        return report, row

    _, original = stage_acquisition("initial-receive", 0)
    assert original.records[0][field] == "20.0"
    report, after_failure = stage_acquisition("truncated", 1)
    assert after_failure == original
    assert report["handoffs"][0]["result"]["acquisition_failures"] == [
        {"source_id": source, "status": "failed", "error_types": ["IndicatorsProviderError"]}
    ]
    capsys.readouterr()
    assert check(["check", "--directory", str(tmp_path / "truncated")]) == 0
    assert json.loads(capsys.readouterr().out)["integration_status"] == "completed_with_source_gaps"
    report, after_complete = stage_acquisition("complete", 0)
    assert after_complete.records[0][field] == missing
    assert after_complete.fetched_at > original.fetched_at
    assert report["handoffs"][0]["result"]["acquisition_failures"] == []


def test_legacy_browser_diagnostic_survives_l1_handoff(tmp_path, setup, monkeypatch, capsys):
    from unittest.mock import Mock

    import baibai_engine.data_collection.acquisition.refresh as acquisition
    from baibai_engine.data_collection.transport.context import (
        BrowserDownloadTimeoutError,
        IndicatorsProviderError,
    )
    from baibai_jobs.integration.economic_handoff import acquire as acquire_source

    secret = "SYNTHETIC_SECRET_MARKER"
    diagnostic = "no download started within 60000 ms; navigation returned HTTP 200"
    legacy_error = BrowserDownloadTimeoutError("legacy attachment timeout", diagnostic=diagnostic)
    legacy_error.__cause__ = TimeoutError(f"https://redirect.example.test/?token={secret}")
    error = IndicatorsProviderError(f"private response {secret}")
    error.__cause__ = legacy_error
    native = Mock(side_effect=error)
    monkeypatch.setattr(acquisition, "fetch_native_records", native)
    database = tmp_path / "source.sqlite"
    with closing(initialize_database(database)) as conn, conn:
        confirm_source_response(
            conn, source_id="fed-h15", fetched_at=AT, records_by_day={DAY: [{"value": "retained"}]}
        )
    directory = tmp_path / "browser-failure"
    assert acquire_source("frb_h15", "", DAY, directory, database) == 1
    source_report = json.loads((directory / "result.json").read_text())
    failure = {
        "source_id": "fed-h15",
        "status": "failed",
        "error_types": ["IndicatorsProviderError", "BrowserDownloadTimeoutError", "TimeoutError"],
        "browser_download_diagnostic": diagnostic,
    }
    assert source_report["sources"] == [{**failure, "history_recovery_required": False}]
    native.assert_called_once()
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
        save_test_handoff(store, group + "-" + name, packet, group)

    for index, value in enumerate("AAB"):
        packet = tmp_path / f"stage-{index}"
        packet.mkdir()
        clock = AT + timedelta(hours=index)
        received(packet / "receipts.sqlite", value, clock)
        publish(packet, str(index), clock)
    namespace = FRED_HANDOFF_NAMESPACE if group == "fred_csv" else "economic-sources"
    immutable = {
        key: value.body
        for key, value in store.values.items()
        if key.startswith(f"coordination/{namespace}/")
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
    marker = f"coordination/{namespace}/{group}-1/integrated.json"
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
    assert next_report["inputs_updated"] == (["economic-boj"] if recovery else [])
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


def _first_retirement(store):
    group = "economic-boj"
    pointer = input_key(group)
    stage_id = json.loads(store.values[pointer].body)["stage_id"]
    return group, pointer, f"coordination/source-inputs/{stage_id}/handoff.json"


@pytest.mark.parametrize("fault", ["503", "read-timeout"])
@pytest.mark.parametrize("resume_boundary", ["delete", "old-manifest"])
def test_detached_retirement_failure_allows_l2_and_other_groups_but_bounds_one_group(
    tmp_path, setup, monkeypatch, fault, resume_boundary
):
    store, _, _ = setup
    run(tmp_path, setup, "first")
    group, pointer, old = _first_retirement(store)
    delete = store.delete_object

    def failure():
        if fault == "503":
            raise R2Error("known transient service reply", http_status=503)
        try:
            raise ReadTimeoutError(endpoint_url="https://private.invalid")
        except ReadTimeoutError as cause:
            raise R2Error("uncertain detached transport") from cause

    def unavailable(key):
        return failure() if key == old else delete(key)

    monkeypatch.setattr(store, "delete_object", unavailable)
    stage(tmp_path, store, name="new-boj", source="boj-bank-lending-yoy", group="boj")
    second = run(tmp_path, setup, "second", asof=DAY + timedelta(days=1))
    assert second["inputs_retirement_pending"] == {group: "verified_successor"}
    assert second["inputs_updated"] == [group]
    b = store.values[pointer]
    assert old in store.values
    fixed = store.get_verified_object
    if resume_boundary == "old-manifest":

        def old_manifest(key, **contract):
            return failure() if key == old else fixed(key, **contract)

        monkeypatch.setattr(store, "get_verified_object", old_manifest)
        monkeypatch.setattr(store, "delete_object", delete)
    third = run(tmp_path, setup, "third", asof=DAY + timedelta(days=2))
    assert third["inputs_retirement_pending"] == {group: "advance_blocked"}
    assert third["inputs_updated"] == []
    assert third["inputs_reused"] == [
        "economic-" + g for g in acquisition_groups() if "economic-" + g != group
    ]
    assert store.values[pointer] is b
    assert old in store.values
    assert (tmp_path / "third.sqlite").read_bytes() == (tmp_path / "second.sqlite").read_bytes()

    # Same selected input may finish an older cleanup without creating another stage.
    monkeypatch.setattr(store, "get_verified_object", fixed)
    monkeypatch.setattr(store, "delete_object", delete)
    before = list(store.put_keys)
    resumed = run(tmp_path, setup, "cleanup", asof=DAY + timedelta(days=2))
    assert "inputs_retirement_pending" not in resumed
    assert resumed["inputs_updated"] == []
    assert resumed["inputs_reused"] == ["economic-" + g for g in acquisition_groups()]
    assert store.put_keys == before
    assert store.values[pointer] is b
    assert old not in store.values


@pytest.mark.parametrize("status", [403, 429, None, "strict"])
def test_unclassified_or_refused_cleanup_never_attempts_another_remote_operation(
    tmp_path, setup, monkeypatch, status
):
    store, _, _ = setup
    run(tmp_path, setup, "first")
    _, _, old = _first_retirement(store)
    stage(tmp_path, store, name="new-boj", source="boj-bank-lending-yoy", group="boj")
    delete = store.delete_object
    observed = []

    def refused(key):
        if key != old:
            return delete(key)
        observed.append((len(store.get_keys), len(store.head_keys), len(store.put_keys)))
        if status == "strict":
            raise R2OperationStopped("confirmed policy stop")
        raise R2Error("external instruction not established", http_status=status)

    monkeypatch.setattr(store, "delete_object", refused)
    expected = R2OperationStopped if status == "strict" else R2Error
    with pytest.raises(expected):
        run(tmp_path, setup, "refused", asof=DAY + timedelta(days=1))
    assert observed == [(len(store.get_keys), len(store.head_keys), len(store.put_keys))]
    assert old in store.values


@pytest.mark.parametrize("fault", ["active-missing", "lease-lost", "retired-429"])
def test_deferred_old_manifest_read_requires_active_successor_and_owner(
    tmp_path, setup, monkeypatch, fault
):
    store, _, handle = setup
    run(tmp_path, setup, "first")
    _, pointer, old = _first_retirement(store)
    delete = store.delete_object

    def defer_once(key):
        if key == old:
            raise R2Error("defer detached deletion", http_status=503)
        return delete(key)

    monkeypatch.setattr(store, "delete_object", defer_once)
    stage(tmp_path, store, name="new-boj", source="boj-bank-lending-yoy", group="boj")
    run(tmp_path, setup, "second", asof=DAY + timedelta(days=1))
    monkeypatch.setattr(store, "delete_object", delete)
    b = store.values[pointer]
    if fault == "active-missing":
        b_stage = json.loads(b.body)["stage_id"]
        del store.values[f"coordination/source-inputs/{b_stage}/handoff.json"]
    fixed = store.get_verified_object
    last_read = []

    def old_manifest(key, **contract):
        if key != old:
            return fixed(key, **contract)
        if fault == "lease-lost":
            release(store, handle)
        last_read.append((len(store.get_keys), len(store.head_keys), len(store.put_keys)))
        raise R2Error(
            "failure on detached manifest", http_status=429 if fault == "retired-429" else 503
        )

    monkeypatch.setattr(store, "get_verified_object", old_manifest)
    with pytest.raises(LeaseError if fault == "lease-lost" else LakePublishError):
        run(tmp_path, setup, "third", asof=DAY + timedelta(days=2))
    assert last_read
    assert len(store.put_keys) == last_read[0][2]
    if fault == "retired-429":
        assert last_read == [(len(store.get_keys), len(store.head_keys), len(store.put_keys))]
    assert store.values[pointer] is b
    assert old in store.values
