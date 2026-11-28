"""公開を止める受入理由だけを表示し、私有の例外本文は出さない。"""

import hashlib
import json
from datetime import UTC, date, datetime

import pytest
from tests.engine.storage.test_market_lake import _dataset_payload, _load_dataset
from tests.helpers.master_calendar_native import pinned_jp_listing
from tests.helpers.r2_store import MemoryR2Store, seed_machine_current

from baibai_engine.storage.snapshots.keys import release_manifest_key
from baibai_engine.storage.snapshots.models import (
    ReleaseDatasetPolicy,
    ReleaseManifest,
    ReleasePolicy,
    canonical_lake_model_bytes,
    validate_release_policy,
)
from baibai_engine.storage.snapshots.release import LakeReleasePointer
from baibai_jobs.integration import models
from baibai_jobs.publication.datasets import LAKE_DATASETS


def fail_acceptance(*, reason):
    manifest = _load_dataset(_dataset_payload())
    if reason == "incomplete_coverage":
        manifest = manifest.model_copy(update={"coverage_status": "partial"})
    release = ReleaseManifest(
        manifest_version=2,
        release_id="synthetic-release",
        profile="production",
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
        data_as_of=manifest.data_as_of,
        datasets={
            manifest.dataset: {
                "build_id": manifest.build_id,
                "layer": manifest.layer,
                "contract_version": manifest.contract_version,
                "manifest_sha256": hashlib.sha256(canonical_lake_model_bytes(manifest)).hexdigest(),
                "data_as_of": manifest.data_as_of,
                "coverage_status": manifest.coverage_status,
                "totals": manifest.totals,
            }
        },
    )
    policy = ReleasePolicy(
        policy_version=1,
        profile="production",
        datasets=(
            ReleaseDatasetPolicy(dataset=manifest.dataset, required=True, carries_history=True),
        ),
        max_manifest_bytes=16 * 1024 * 1024,
        max_objects=10_000,
    )
    validate_release_policy(
        release,
        {manifest.dataset: manifest},
        catalog=LAKE_DATASETS,
        policy=policy,
        published_coverage_start={manifest.dataset: date(2026, 7, 1)}
        if reason == "history_floor_moved"
        else None,
    )
    pytest.fail("the original acceptance condition must still reject")


def run_failure(tmp_path, monkeypatch, error):
    remote = MemoryR2Store()

    pointer = seed_machine_current(
        remote,
        LakeReleasePointer(
            release_id="fixture-current",
            manifest_key=release_manifest_key(release_id="fixture-current"),
            manifest_sha256="b" * 64,
        ),
    )

    as_of = date(2026, 11, 16)
    listing_path = tmp_path / "current-listing-master.json"
    current_master = pinned_jp_listing(as_of=as_of, output=listing_path)
    calls = []

    def fail(**kwargs):
        calls.append((kwargs["base"].pointer, kwargs["current_master"]))
        if callable(error):
            error()
        else:
            raise error

    monkeypatch.setattr(models, "Boto3R2Store", lambda **_: remote)
    monkeypatch.setattr(models, "integrate_models", fail)
    notice = tmp_path / "private-notice.json"
    assert (
        models.main(
            [
                "--asof",
                as_of.isoformat(),
                "--current-listing-master",
                str(listing_path),
                "--lease-handle",
                str(tmp_path / "unused"),
                "--notice-output",
                str(notice),
            ]
        )
        == 1
    )
    assert calls == [(pointer, current_master)]
    return json.loads(notice.read_bytes())


@pytest.mark.parametrize("reason", ["incomplete_coverage", "history_floor_moved"])
def test_normal_failure_notice_names_only_the_acceptance_reason(
    reason, tmp_path, monkeypatch, capsys
):
    notice = run_failure(tmp_path, monkeypatch, lambda: fail_acceptance(reason=reason))
    expected = {"reason": reason, "dataset": "jquants.daily_bars"}
    assert notice["release_acceptance"] == expected
    assert notice["models_ready"] is False
    output = capsys.readouterr()
    assert output.out == ""
    assert '"reason": "' + reason + '"' in output.err
    assert '"dataset": "jquants.daily_bars"' in output.err
    assert "2026-07-01" not in output.err


def test_unclassified_exception_keeps_its_body_private(tmp_path, monkeypatch, capsys):
    secret = "private-token-path-request-and-document\nsecret"
    notice = run_failure(tmp_path, monkeypatch, ValueError(secret))
    output = capsys.readouterr()
    assert "release_acceptance" not in notice
    assert notice["error_type"] == "ValueError"
    assert secret not in json.dumps(notice) + output.out + output.err


@pytest.mark.parametrize("dataset", ["jquants.daily_bars", None, "private-token\nunknown-dataset"])
def test_typed_failure_does_not_disclose_text_or_unregistered_dataset(
    dataset, tmp_path, monkeypatch, capsys
):
    from baibai_engine.storage.snapshots.models import ReleasePolicyError, ReleasePolicyReason

    secret = "private-token-path-request-document-and-receipt"
    error = ReleasePolicyError(
        secret, reason=ReleasePolicyReason.HISTORY_FLOOR_MOVED, dataset=dataset
    )
    notice = run_failure(tmp_path, monkeypatch, error)
    output = capsys.readouterr()
    expected = {"reason": "history_floor_moved"}
    if dataset == "jquants.daily_bars":
        expected["dataset"] = dataset
    assert notice["release_acceptance"] == expected
    assert secret not in json.dumps(notice) + output.out + output.err
    assert "private-token" not in json.dumps(notice) + output.out + output.err


def test_reason_must_be_an_owner_defined_code():
    from baibai_engine.storage.snapshots.models import ReleasePolicyError

    with pytest.raises(ValueError, match="not a valid ReleasePolicyReason"):
        ReleasePolicyError("private message", reason="private-unregistered-reason")
