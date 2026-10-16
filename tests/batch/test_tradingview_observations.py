from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml
from tests.engine.test_tradingview_accepted import document, input_file
from tests.engine.test_tradingview_collector import payload
from tests.helpers.l1_release import market_store
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.r2_store import MemoryR2Store

from baibai_batch.jobs.tradingview_observations import DATASET, main, publish_and_verify, stage
from baibai_batch.storage import publish_market_lake as publisher
from baibai_batch.storage import lake_publish
from baibai_batch.storage.publication_lease import LeaseError, acquire, release
from baibai_engine.external_assessment.tradingview.accepted import (
    accept_observations,
    load_observations,
)
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.hydrate import dehydrate_market_store, hydrate_market_store
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/cloud-tradingview-snapshot.yml"


def event_file(tmp_path, value=None):
    raw = json.dumps(document() if value is None else value, sort_keys=True, separators=(",", ":"))
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "inputs": {
                    "observations_json": raw,
                    "observations_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                }
            }
        )
    )
    return event


def test_stage_preserves_input_bytes_and_reports_only_provenance(tmp_path, capsys):
    event = event_file(tmp_path)
    stage(event, tmp_path / "stage")
    expected = json.loads(event.read_text())["inputs"]
    assert (tmp_path / "stage/observations.json").read_text() == expected["observations_json"]
    assert (tmp_path / "stage/input.sha256").read_text() == expected["observations_sha256"]
    assert (tmp_path / "stage/observations.json").stat().st_mode & 0o777 == 0o600
    result = json.loads(capsys.readouterr().out)
    assert result["rows"] == 3
    assert result["source_ref"] == document()["source_ref"]
    assert "TSE:" not in json.dumps(result)
    assert "earnings_per_share" not in json.dumps(result)


def test_invalid_stage_never_echoes_body(tmp_path, capsys):
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "inputs": {
                    "observations_json": "private-token https://private",
                    "observations_sha256": "a" * 64,
                }
            }
        )
    )
    assert main(["stage", "--event", str(event), "--directory", str(tmp_path / "stage")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private" not in captured.err
    assert "https://" not in captured.err


@pytest.mark.parametrize(
    ("event", "ref", "raw", "digest", "normal", "accept", "basis"),
    [
        ("schedule", "refs/heads/main", "", "", True, False, ""),
        ("workflow_dispatch", "refs/heads/main", "", "", True, False, ""),
        ("workflow_dispatch", "refs/heads/main", "json", "hash", False, True, ""),
        ("workflow_dispatch", "refs/heads/main", "json", "", False, True, ""),
        ("workflow_dispatch", "refs/heads/main", "", "hash", False, True, ""),
        ("workflow_dispatch", "refs/heads/other", "json", "hash", False, False, ""),
        ("workflow_dispatch", "refs/heads/main", "", "", True, False, "2026-10-09"),
        ("workflow_dispatch", "refs/heads/other", "", "", False, False, "2026-10-09"),
    ],
)
def test_workflow_routes_empty_inputs_to_normal_collection(
    event, ref, raw, digest, normal, accept, basis
):
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)

    def evaluate(condition):
        expression = condition.removeprefix("${{").removesuffix("}}")
        for key, value in {
            "github.event_name": event,
            "github.ref": ref,
            "inputs.observations_json": raw,
            "inputs.observations_sha256": digest,
            "inputs.observation_master_asof": basis,
        }.items():
            expression = expression.replace(key, repr(value))
        return eval(
            expression.replace("&&", " and ").replace("||", " or "), {"__builtins__": {}}, {}
        )

    assert evaluate(workflow["jobs"]["snapshot"]["if"]) is normal
    assert evaluate(workflow["jobs"]["accept-observations"]["if"]) is accept


def test_acceptance_workflow_is_main_manual_only_and_never_exposes_raw_or_oauth():
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["accept-observations"]
    assert job["environment"] == "tradingview-runtime"
    steps = job["steps"]
    ordered = [step.get("id", step.get("name")) for step in steps]
    assert ordered.index("Validate saved response input") < ordered.index("lease-acquire")
    assert (
        ordered.index("lease-acquire")
        < ordered.index("pull")
        < ordered.index("publish-lake")
        < ordered.index("lease-release")
    )
    source = json.dumps(job)
    assert "TRADINGVIEW_OAUTH_STATE" not in source
    assert "TRADINGVIEW_SECRET_WRITER_TOKEN" not in source
    assert "push-market" not in source
    publish_step = next(step for step in steps if step.get("id") == "publish-lake")
    assert '--lease-handle "$RUNNER_TEMP/publication-lease-handle.json"' in publish_step["run"]
    for step in steps:
        assert "inputs.observations" not in step.get("run", "")
        assert "inputs.observations" not in json.dumps(step.get("env", {}))
    release = next(step for step in steps if step.get("id") == "lease-release")
    assert "always()" in release["if"]
    assert "acquired == 'true'" in release["if"]


@pytest.mark.parametrize("lose_lease", [False, True])
def test_fresh_hydrate_publish_and_remote_readback_preserve_history_and_other_datasets(
    tmp_path, monkeypatch, lose_lease
):
    datasets = ("jquants.daily_bars", "jquants.short_sale_reports", DATASET)
    narrow_release_policy(monkeypatch, datasets=datasets)
    monkeypatch.setattr(publisher, "lake_verified_git_commit", lambda: "a" * 40)
    source_path = market_store(tmp_path / "initial.sqlite")
    older = document()
    older["observation_date_jst"] = "2026-10-09"
    older["observations"] = [older["observations"][0]]
    older["observations"][0]["timing"] = {
        "start": "2026-10-09T07:00:00Z",
        "end": "2026-10-09T07:00:01Z",
    }
    older["observations"][0]["result"] = payload(["TSE:1003"])
    input_path, digest = input_file(tmp_path, older)
    accept_observations(source_path, load_observations(input_path, sha256=digest))
    remote = MemoryR2Store()
    mirror = tmp_path / "mirror"
    publisher.publish_market_lake(
        sqlite_path=source_path, mirror_root=mirror, store=remote, release_id="before-observations"
    )
    pointer = publisher._serving_pointer(remote).pointer
    before = publisher._resolve_serving_release(remote, mirror, pointer)
    dehydrate_market_store(source_path, release=before)
    working = tmp_path / "fresh.sqlite"
    shutil.copy2(source_path, working)
    with lake_session() as session:
        hydrate_market_store(
            session,
            release=before,
            cache=LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)),
            store=working,
            dataset_names=datasets,
        )
    handle, _ = acquire(remote, "tradingview")
    unrelated = {
        obj.key for name, manifest in before.dataset_manifests.items() if name != DATASET
        for part in manifest.partitions for obj in part.objects
    }
    download = remote.download_file
    def download_without_unrelated_data(key, path, *, expect_bytes=None):
        if not lose_lease:
            assert key not in unrelated, "unchanged unrelated Parquet must not be downloaded"
        return download(key, path, expect_bytes=expect_bytes)
    monkeypatch.setattr(remote, "download_file", download_without_unrelated_data)
    directory = tmp_path / "accepted"
    stage(event_file(tmp_path), directory)
    if lose_lease:
        ensure = lake_publish._ensure_immutable
        released = False
        def release_during_upload(*args, **kwargs):
            nonlocal released
            result = ensure(*args, **kwargs)
            if not released:
                release(remote, handle)
                released = True
            return result
        monkeypatch.setattr(lake_publish, "_ensure_immutable", release_during_upload)
        with pytest.raises(LeaseError):
            publish_and_verify(directory, sqlite_path=working, mirror=mirror, store=remote, handle=handle)
        assert released
        assert publisher._serving_pointer(remote).pointer == pointer
        assert not (directory / "readback-report.json").exists()
        return
    report = publish_and_verify(directory, sqlite_path=working, mirror=mirror, store=remote, handle=handle)
    assert report["rows_added"] == 3
    assert report["retained_rows"] == 1
    assert report["total_rows"] == 4
    assert report["readback"] == "verified"
    assert not (directory / "readback.sqlite").exists()
    assert report["unchanged_other_datasets"] == 2
    assert report["coverage_status"] == "partial"
    assert report["release_id"] != before.release_id
    # A retry from a fresh acceptance directory confirms the same release and makes no PUT.
    retry = tmp_path / "retry"
    stage(event_file(tmp_path), retry)
    old_pointer = publisher._serving_pointer(remote).pointer
    old_put_count = len(remote.put_keys)
    report2 = publish_and_verify(retry, sqlite_path=working, mirror=mirror, store=remote, handle=handle)
    assert report2["rows_added"] == 0
    assert report2["status"] == "already_recorded"
    assert publisher._serving_pointer(remote).pointer == old_pointer
    assert len(remote.put_keys) == old_put_count


def test_empty_observation_document_is_not_replaced_by_fixture_defaults(tmp_path, capsys):
    event = event_file(tmp_path, {})
    assert main(["stage", "--event", str(event), "--directory", str(tmp_path / "stage")]) == 1
    assert capsys.readouterr().out == ""
