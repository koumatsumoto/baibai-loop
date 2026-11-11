"""Daily input dispatch never republishes retired EDINET planning inputs."""

import gzip
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.tradingview import DAY
from tests.helpers.tradingview import store as market_store

from baibai_engine.data_collection.providers.edinet.facts_scope import (
    FactsScope,
    merge_scope,
)
from baibai_engine.storage.snapshots.keys import current_release_pointer_key, release_manifest_key
from baibai_engine.storage.snapshots.models import canonical_lake_model_bytes
from baibai_engine.storage.snapshots.release import LakeReleasePointer
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition import source_inputs
from baibai_jobs.acquisition.article_planning import ARTICLE_SOURCES, load_planning
from baibai_jobs.acquisition.inputs import SOURCE_INPUT_GROUPS
from baibai_jobs.acquisition.source_inputs import input_key, load_input
from baibai_jobs.integration import sources
from baibai_jobs.publication.lease import (
    PUBLICATION_LEASE_KEY,
    _load_handle,
    _save_handle,
    acquire,
    release,
)

GENERATIONS = {"lake_release_id": "fixed", "lake_manifest_sha256": "a" * 64}


def _input_cli(tmp_path, monkeypatch, *, lease_now=None):
    remote = MemoryR2Store()
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: remote)
    market = market_store(tmp_path / "market.sqlite", 2)
    with closing(open_connection(market)) as conn, conn:
        conn.execute(
            "INSERT INTO edinet_list_responses VALUES(?,?,?,?)",
            ("receipt", str(DAY), f"{DAY}T00:00:00+00:00", gzip.compress(b"{}", mtime=0)),
        )
        conn.executemany(
            "INSERT INTO edinet_source_archives VALUES(?,?,?,?)",
            [("S100CSV", 5, str(DAY), b"csv zip"), ("S100XBRL", 1, str(DAY), b"xbrl zip")],
        )
        merge_scope(conn, FactsScope(DAY, frozenset({"S100XBRL"}), frozenset()))
    for key in ("market.sqlite", "runs.sqlite"):
        remote.store_object(key, f"fixed {key}".encode())
    remote.store_object(
        "machine-manifest.json",
        json.dumps(
            {
                "schema_version": 1,
                "keys": {key: remote.values[key].etag for key in ("market.sqlite", "runs.sqlite")},
            }
        ).encode(),
    )
    remote.store_object(
        current_release_pointer_key(),
        canonical_lake_model_bytes(
            LakeReleasePointer(
                release_id=GENERATIONS["lake_release_id"],
                manifest_key=release_manifest_key(release_id=GENERATIONS["lake_release_id"]),
                manifest_sha256=GENERATIONS["lake_manifest_sha256"],
            )
        ),
    )
    handle, _ = acquire(remote, "daily", now=lease_now)
    lease_path = tmp_path / "lease.json"
    _save_handle(lease_path, handle)
    remote.put_keys.clear()
    command = [
        "inputs",
        "--directory",
        str(tmp_path / "integration"),
        "--market",
        str(market),
        "--identity",
        "cycle",
        "--asof",
        str(DAY),
        "--lease-handle",
        str(lease_path),
    ]
    return remote, market, command


def test_daily_cli_publishes_every_supported_input_and_reads_back_fixed_generation(
    tmp_path, monkeypatch, capsys
):
    remote, _, command = _input_cli(tmp_path, monkeypatch)
    expected = list(SOURCE_INPUT_GROUPS)
    assert sources.main(command) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"updated": expected + list(ARTICLE_SOURCES), "failed": []}
    assert {
        key
        for key in remote.values
        if key.startswith("coordination/source-inputs/") and key.count("/") == 2
    } == {input_key(group) for group in expected}
    for group in expected:
        database = tmp_path / f"{group}.sqlite"
        result = load_input(remote, group, database, tmp_path / f"read-{group}")
        assert result["canonical_generations"] == GENERATIONS
        assert result["group"] == group
        assert result["asof"] == str(DAY)
    for source in ARTICLE_SOURCES:
        assert load_planning(remote, source)[0].generations == GENERATIONS
    assert not any("economic-" in key for key in remote.put_keys)


@pytest.mark.parametrize("group", ["edinet-list", "edinet-metrics", "edinet-facts"])
def test_explicit_edinet_input_is_rejected(tmp_path, monkeypatch, capsys, group):
    remote, _, command = _input_cli(tmp_path, monkeypatch)
    assert sources.main([*command, "--group", group]) == 1
    assert "ValueError" in capsys.readouterr().err
    assert remote.put_keys == []


def test_native_cli_reports_failed_group_and_preserves_its_previous_input(
    tmp_path, monkeypatch, capsys
):
    remote, _market, command = _input_cli(tmp_path, monkeypatch)
    assert sources.main(command) == 0
    capsys.readouterr()
    prior = remote.values[input_key("tradingview")]

    def failed_export(*_, **kwargs):
        assert kwargs["group"] == "tradingview"
        raise ValueError("synthetic source-input export failure")

    monkeypatch.setattr(
        "baibai_jobs.acquisition.source_inputs.export_market_source_input", failed_export
    )
    command[command.index("--identity") + 1] = "retry"
    assert sources.main(command) == 1
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "updated": [group for group in SOURCE_INPUT_GROUPS if group != "tradingview"]
        + list(ARTICLE_SOURCES),
        "failed": [{"group": "tradingview", "reason": "ValueError"}],
    }
    assert remote.values[input_key("tradingview")] == prior
    assert not any(key.startswith("coordination/source-inputs/edinet-") for key in remote.values)


def test_daily_input_publication_leaves_retired_source_inputs_untouched(
    tmp_path, monkeypatch, capsys
):
    remote, _, command = _input_cli(tmp_path, monkeypatch)
    previous = {}
    for group in (
        "jpx-earnings",
        "jpx-regulations",
        "edinet-list",
        "edinet-metrics",
        "edinet-facts",
    ):
        for key in (
            f"coordination/source-inputs/{group}.json",
            f"coordination/source-inputs/{group}-old/handoff.json",
        ):
            remote.store_object(key, b"retained old input")
            previous[key] = remote.values[key]
    remote.head_keys.clear()
    remote.get_keys.clear()
    remote.put_keys.clear()
    assert sources.main(command) == 0
    report = json.loads(capsys.readouterr().out)
    assert not any(group.startswith(("jpx-", "edinet-")) for group in report["updated"])
    assert not any(
        key.startswith(("coordination/source-inputs/jpx-", "coordination/source-inputs/edinet-"))
        for key in remote.head_keys + remote.get_keys + remote.put_keys
    )
    assert {key: remote.values[key] for key in previous} == previous


@pytest.mark.parametrize("changed_key", ["market.sqlite", "runs.sqlite"])
def test_native_cli_does_not_publish_inputs_against_a_partial_machine_bundle(
    tmp_path, monkeypatch, capsys, changed_key
):
    remote, _, command = _input_cli(tmp_path, monkeypatch)
    remote.store_object(changed_key, b"partial replacement")
    assert sources.main(command) == 1
    assert "LakePublishError" in capsys.readouterr().err
    assert remote.put_keys == []


def _seal_cli(tmp_path, lease_path):
    directory = tmp_path / "integration"
    directory.mkdir(exist_ok=True)
    (directory / "applied.json").write_text(
        json.dumps(
            {
                "handoffs": [
                    {
                        "entry": {
                            "market": True,
                            "stage_id": f"jpx-guard-{index}",
                            "source": "jpx-earnings",
                            "manifest_sha256": str(index) * 64,
                            "started_at": f"{DAY}T00:0{index}:00+00:00",
                        },
                        "applied": True,
                        "result": {"retained_bundles": []},
                        "recovery": False,
                    }
                    for index in range(2)
                ]
            }
        )
    )
    return ["seal", "--directory", str(directory), "--lease-handle", str(lease_path)]


@pytest.mark.parametrize("command_name", ["inputs", "seal"])
@pytest.mark.parametrize("state", ["missing", "malformed", "expired", "released"])
def test_shared_source_state_refuses_invalid_lease_before_any_write(
    tmp_path, monkeypatch, command_name, state
):
    expired = datetime.now(UTC) - timedelta(days=1) if state == "expired" else None
    remote, _, command = _input_cli(tmp_path, monkeypatch, lease_now=expired)
    path = tmp_path / "lease.json"
    if state == "missing":
        path.unlink()
    elif state == "malformed":
        path.write_text("{")
    elif state == "released":
        release(remote, _load_handle(path))
    if command_name == "seal":
        command = _seal_cli(tmp_path, path)
    remote.put_keys.clear()
    assert sources.main(command) == 1
    assert remote.put_keys == []


@pytest.mark.parametrize("timing", ["during_export", "after_receipt_put"])
def test_input_publication_stops_next_put_after_release_and_can_reissue(
    tmp_path, monkeypatch, timing
):
    remote, _, command = _input_cli(tmp_path, monkeypatch)
    handle = _load_handle(tmp_path / "lease.json")
    put = remote.put_file
    export = source_inputs.export_market_source_input
    released = False

    def during_export(*args, **kwargs):
        nonlocal released
        if not released:
            released = True
            release(remote, handle)
        return export(*args, **kwargs)

    def after_receipt(key, *args, **kwargs):
        nonlocal released
        result = put(key, *args, **kwargs)
        if (
            not released
            and key.startswith("coordination/source-inputs/")
            and key.endswith(".sqlite")
        ):
            released = True
            release(remote, handle)
        return result

    if timing == "during_export":
        monkeypatch.setattr(source_inputs, "export_market_source_input", during_export)
    else:
        monkeypatch.setattr(remote, "put_file", after_receipt)
    command += ["--group", "tradingview"]
    assert sources.main(command) == 1
    written = [key for key in remote.put_keys if key != PUBLICATION_LEASE_KEY]
    assert len(written) == int(timing == "after_receipt_put")
    assert all(key.endswith(".sqlite") for key in written)
    assert input_key("tradingview") not in remote.values
    retained = {key: remote.values[key] for key in written}

    next_handle, _ = acquire(remote, "operator")
    path = tmp_path / "next-lease.json"
    _save_handle(path, next_handle)
    command[command.index("--lease-handle") + 1] = str(path)
    command[command.index("--identity") + 1] = "reissue"
    assert sources.main(command) == 0
    assert input_key("tradingview") in remote.values
    assert {key: remote.values[key] for key in retained} == retained


def test_ordered_market_seals_stop_after_release_and_resume_without_rewriting(
    tmp_path, monkeypatch
):
    remote, _, _ = _input_cli(tmp_path, monkeypatch)
    command = _seal_cli(tmp_path, tmp_path / "lease.json")
    handle = _load_handle(tmp_path / "lease.json")
    put = remote.put_file
    released = False

    def after_first_marker(key, *args, **kwargs):
        nonlocal released
        result = put(key, *args, **kwargs)
        if key.endswith("/integrated.json") and not released:
            released = True
            release(remote, handle)
        return result

    monkeypatch.setattr(remote, "put_file", after_first_marker)
    assert sources.main(command) == 1
    markers = [key for key in remote.put_keys if key != PUBLICATION_LEASE_KEY]
    assert markers == ["coordination/market-sources/jpx-guard-0/integrated.json"]
    original = remote.values[markers[0]]
    assert "coordination/market-sources/jpx-guard-1/integrated.json" not in remote.values

    next_handle, _ = acquire(remote, "operator")
    path = tmp_path / "next-lease.json"
    _save_handle(path, next_handle)
    command[command.index("--lease-handle") + 1] = str(path)
    remote.put_keys.clear()
    assert sources.main(command) == 0
    assert remote.put_keys == ["coordination/market-sources/jpx-guard-1/integrated.json"]
    assert remote.values[markers[0]] == original
