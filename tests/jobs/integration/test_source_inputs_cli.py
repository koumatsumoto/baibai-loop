"""Daily input dispatch never republishes retired EDINET planning inputs."""

import gzip
import json
from contextlib import closing

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
from baibai_jobs.acquisition.article_planning import ARTICLE_SOURCES, load_planning
from baibai_jobs.acquisition.inputs import SOURCE_INPUT_GROUPS
from baibai_jobs.acquisition.source_inputs import input_key, load_input
from baibai_jobs.integration import sources
from baibai_jobs.publication.lease import _save_handle, acquire

GENERATIONS = {"lake_release_id": "fixed", "lake_manifest_sha256": "a" * 64}


def _input_cli(tmp_path, monkeypatch, native):
    if native is None:
        monkeypatch.delenv("BAIBAI_EDINET_LIST_NATIVE", raising=False)
    else:
        monkeypatch.setenv("BAIBAI_EDINET_LIST_NATIVE", native)
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
    handle, _ = acquire(remote, "daily")
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


@pytest.mark.parametrize("native", ["true", "false", None])
def test_daily_cli_publishes_every_supported_input_and_reads_back_fixed_generation(
    tmp_path, monkeypatch, capsys, native
):
    remote, _, command = _input_cli(tmp_path, monkeypatch, native)
    expected = [group for group in SOURCE_INPUT_GROUPS if not group.startswith("edinet-")]
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


@pytest.mark.parametrize("native", ["true", "false", None])
def test_explicit_list_input_is_rejected_even_with_old_false_environment(
    tmp_path, monkeypatch, capsys, native
):
    remote, _, command = _input_cli(tmp_path, monkeypatch, native)
    assert sources.main([*command, "--group", "edinet-list"]) == 1
    assert "ValueError" in capsys.readouterr().err
    assert remote.put_keys == []


def test_native_cli_reports_failed_group_and_preserves_its_previous_input(
    tmp_path, monkeypatch, capsys
):
    remote, market, command = _input_cli(tmp_path, monkeypatch, "true")
    assert sources.main(command) == 0
    capsys.readouterr()
    prior = remote.values[input_key("tradingview")]
    with closing(open_connection(market)) as conn, conn:
        conn.execute("DELETE FROM jquants_market_calendar")
    command[command.index("--identity") + 1] = "retry"
    assert sources.main(command) == 1
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "updated": [
            group
            for group in SOURCE_INPUT_GROUPS
            if not group.startswith("edinet-") and group != "tradingview"
        ]
        + list(ARTICLE_SOURCES),
        "failed": [{"group": "tradingview", "reason": "ValueError"}],
    }
    assert remote.values[input_key("tradingview")] == prior
    assert input_key("edinet-list") not in remote.values
    for group in ("edinet-metrics", "edinet-facts"):
        assert input_key(group) not in remote.values


@pytest.mark.parametrize("native", ["true", "false", None])
def test_daily_input_publication_leaves_retired_source_inputs_untouched(
    tmp_path, monkeypatch, capsys, native
):
    remote, _, command = _input_cli(tmp_path, monkeypatch, native)
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
    remote, _, command = _input_cli(tmp_path, monkeypatch, "true")
    remote.store_object(changed_key, b"partial replacement")
    assert sources.main(command) == 1
    assert "LakePublishError" in capsys.readouterr().err
    assert remote.put_keys == []
