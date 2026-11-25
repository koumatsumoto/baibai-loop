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
from baibai_jobs.acquisition.article_planning import COLLECTION_SOURCES, load_planning, source_key
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
    monkeypatch.setattr(remote, "close", lambda: None, raising=False)
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
    assert sources.main(command) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"updated": list(COLLECTION_SOURCES), "failed": []}
    assert not any(key.startswith("coordination/source-inputs/") for key in remote.values)
    for source in COLLECTION_SOURCES:
        assert load_planning(remote, source)[0].generations == GENERATIONS
    assert not any("economic-" in key for key in remote.put_keys)


@pytest.mark.parametrize("command", ["plan", "apply", "seal", "check"])
def test_group_cannot_turn_a_scoped_retry_into_an_unscoped_command(
    tmp_path, monkeypatch, capsys, command
):
    def unexpected_store(**_):
        pytest.fail("a rejected scope must not access the remote store")

    monkeypatch.setattr(sources, "Boto3R2Store", unexpected_store)
    work = tmp_path / "untouched"
    assert sources.main([command, "--directory", str(work), "--group", "tradingview"]) == 1
    assert "--group is only valid with inputs" in capsys.readouterr().err
    assert not work.exists()


def test_reissue_only_failed_article_input_without_touching_other_plans(tmp_path, monkeypatch):
    remote, _, command = _input_cli(tmp_path, monkeypatch)
    failed = "boj_news"
    publish = sources.publish_planning_input

    def fail_boj(store, source, generations):
        if source == failed:
            raise ValueError("simulated planning failure")
        publish(store, source, generations)

    monkeypatch.setattr(sources, "publish_planning_input", fail_boj)
    assert sources.main(command) == 1
    assert source_key(failed) not in remote.values
    assert "coordination/source-inputs/tradingview.json" not in remote.values
    before = dict(remote.values)
    monkeypatch.setattr(sources, "publish_planning_input", publish)
    remote.get_keys.clear()
    remote.head_keys.clear()
    remote.put_keys.clear()
    assert sources.main([*command, "--group", failed]) == 0
    assert remote.put_keys == [source_key(failed)]
    assert {key: remote.values[key] for key in before} == before
    assert not {
        key
        for key in remote.get_keys + remote.head_keys
        if key.startswith(("coordination/article-planning/", "coordination/source-inputs/"))
        and key != source_key(failed)
    }
    assert load_planning(remote, failed)[0].generations == GENERATIONS


@pytest.mark.parametrize("group", ["edinet-list", "edinet-metrics", "edinet-facts", "tradingview"])
def test_explicit_inactive_input_is_rejected(tmp_path, monkeypatch, capsys, group):
    remote, _, command = _input_cli(tmp_path, monkeypatch)
    assert sources.main([*command, "--group", group]) == 1
    assert "ValueError" in capsys.readouterr().err
    assert remote.put_keys == []


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
        "tradingview",
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
    assert not any(
        group.startswith(("jpx-", "edinet-", "tradingview")) for group in report["updated"]
    )
    assert not any(
        key.startswith("coordination/source-inputs/")
        for key in remote.head_keys + remote.get_keys + remote.put_keys
    )
    assert {key: remote.values[key] for key in previous} == previous


def test_daily_inputs_leave_on_demand_news_planning_untouched(tmp_path, monkeypatch, capsys):
    remote, _, command = _input_cli(tmp_path, monkeypatch)
    keys = {source_key(source) for source in ("minkabu_news", "rakuten_news")}
    for key in keys:
        remote.store_object(key, b"retained old planning")
    previous = {key: remote.values[key] for key in keys}
    remote.head_keys.clear()
    remote.get_keys.clear()
    remote.put_keys.clear()
    assert sources.main(command) == 0
    report = json.loads(capsys.readouterr().out)
    assert not {"minkabu_news", "rakuten_news"}.intersection(report["updated"])
    assert not keys.intersection(remote.head_keys + remote.get_keys + remote.put_keys)
    assert {key: remote.values[key] for key in keys} == previous


@pytest.mark.parametrize("source", ["minkabu_news", "rakuten_news"])
def test_explicit_input_does_not_recreate_retired_article_planning(
    tmp_path, monkeypatch, capsys, source
):
    remote, _, command = _input_cli(tmp_path, monkeypatch)
    remote.head_keys.clear()
    remote.get_keys.clear()
    assert sources.main([*command, "--group", source]) == 1
    assert "ValueError" in capsys.readouterr().err
    assert not (remote.head_keys + remote.get_keys + remote.put_keys)
    assert source_key(source) not in remote.values


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
