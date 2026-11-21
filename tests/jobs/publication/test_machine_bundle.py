"""Scoped inputs and a display-only recovery share one published machine proof."""

import hashlib
import json
from contextlib import closing

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.snapshots.keys import current_release_pointer_key, release_manifest_key
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    write_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.article_planning import load_planning, source_key
from baibai_jobs.integration import sources
from baibai_jobs.publication import machine_bundle
from baibai_jobs.publication.lease import _save_handle, acquire

MACHINE_KEYS = ("market.sqlite", "runs.sqlite", "indicators.sqlite")


def _publish_pointer(remote: MemoryR2Store, release_id: str, digest: str) -> None:
    remote.store_object(
        current_release_pointer_key(),
        json.dumps(
            {
                "release_id": release_id,
                "manifest_key": release_manifest_key(release_id=release_id),
                "manifest_sha256": digest,
            }
        ).encode(),
    )


def _remote_bundle() -> MemoryR2Store:
    remote = MemoryR2Store()
    _publish_pointer(remote, "published", "a" * 64)
    for key in MACHINE_KEYS:
        remote.store_object(key, f"initial {key}".encode())
    _publish_receipt(remote)
    return remote


def _publish_receipt(remote: MemoryR2Store, *, origin=True) -> None:
    receipt = {
        "schema_version": 1,
        "keys": {key: remote.values[key].etag for key in MACHINE_KEYS},
    }
    if origin is True:
        receipt["daily_publication"] = {
            "run": "12345-1",
            "release_id": "published",
            "release_manifest_sha256": "a" * 64,
        }
    elif isinstance(origin, dict):
        receipt["daily_publication"] = origin
    remote.store_object("machine-manifest.json", json.dumps(receipt).encode())


@pytest.mark.parametrize("changed_key", ["market.sqlite", "runs.sqlite"])
def test_published_generation_refuses_any_partial_machine_push(
    tmp_path, monkeypatch: pytest.MonkeyPatch, changed_key: str
) -> None:
    remote = _remote_bundle()
    old_economic = remote.values["indicators.sqlite"].etag
    generation_dir = tmp_path / "pulled-generations"
    generation_dir.mkdir()
    sidecar = generation_dir / "indicators.sqlite.etag"
    sidecar.write_text(old_economic)
    monkeypatch.setenv("R2_GENERATION_DIR", str(generation_dir))

    # The bytes of a no-op SQLite snapshot can move independently of its rows.
    remote.store_object(changed_key, f"new snapshot bytes for {changed_key}".encode())
    with pytest.raises(LakePublishError, match="differs from its receipt"):
        sources.canonical_generations(remote)
    _publish_receipt(remote)
    generations = sources.canonical_generations(remote)
    assert generations == {
        "lake_release_id": "published",
        "lake_manifest_sha256": "a" * 64,
    }
    assert sidecar.read_text() == old_economic


def test_inputs_cli_stops_before_publishing_when_machine_receipt_is_stale(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = _remote_bundle()
    monkeypatch.setattr(remote, "close", lambda: None, raising=False)
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: remote)
    monkeypatch.setattr(sources, "_load_handle", lambda _: object())
    monkeypatch.setattr(sources, "verify_held", lambda *_: None)
    published = []

    def publish(*_args, **kwargs):
        published.append(kwargs["generations"])
        return {"updated": ["tradingview"], "failed": []}

    monkeypatch.setattr(sources, "publish_inputs", publish)
    remote.store_object("indicators.sqlite", b"same logical economic rows, resealed bytes")
    _publish_receipt(remote)
    command = [
        "inputs",
        "--directory",
        str(tmp_path / "integration"),
        "--lease-handle",
        str(tmp_path / "lease.json"),
        "--asof",
        "2026-10-30",
        "--identity",
        "synthetic",
        "--group",
        "tradingview",
    ]
    assert sources.main(command) == 0
    assert "economic_etag" not in published[0]

    remote.store_object("runs.sqlite", b"partial machine upload")
    assert sources.main(command) == 1
    assert len(published) == 1


@pytest.mark.parametrize(
    ("release_id", "digest"),
    [("successor", "b" * 64), ("successor", "a" * 64), ("published", "b" * 64)],
)
def test_scoped_inputs_refuse_an_unpaired_daily_release(tmp_path, monkeypatch, release_id, digest):
    remote = _remote_bundle()
    monkeypatch.setattr(remote, "close", lambda: None, raising=False)
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: remote)
    handle, _ = acquire(remote, "operator")
    lease = tmp_path / "lease.json"
    _save_handle(lease, handle)
    command = [
        "inputs",
        "--directory",
        str(tmp_path / "integration"),
        "--lease-handle",
        str(lease),
        "--asof",
        "2026-10-30",
        "--identity",
        "synthetic",
        "--group",
        "boj_news",
    ]

    # model-integration can publish the lake before the machine transfer completes.
    # Both old store ETags still match their receipt, but neither proves this release.
    _publish_pointer(remote, release_id, digest)
    prior = dict(remote.values)
    remote.put_keys.clear()
    assert sources.main(command) == 1
    assert not remote.put_keys
    assert remote.values == prior

    for key in ("market.sqlite", "runs.sqlite"):
        remote.store_object(key, f"successor {key}".encode())
    _publish_receipt(
        remote,
        origin={"run": "12346-1", "release_id": release_id, "release_manifest_sha256": digest},
    )
    assert sources.main(command) == 0
    assert remote.put_keys == [source_key("boj_news")]
    assert load_planning(remote, "boj_news")[0].generations == {
        "lake_release_id": release_id,
        "lake_manifest_sha256": digest,
    }


def test_economic_change_does_not_invalidate_market_bundle():
    remote = _remote_bundle()
    before = sources.canonical_generations(remote)
    remote.store_object("indicators.sqlite", b"new independently confirmed economic L1")
    assert sources.canonical_generations(remote) == before
    assert "economic_etag" not in before


@pytest.mark.parametrize(
    "other", [None, "wrong-run", "wrong-attempt", "wrong-lake", "wrong-digest"]
)
def test_current_receipt_digest_cannot_impersonate_the_original_daily_run(
    tmp_path, monkeypatch, other
):
    remote = _remote_bundle()
    origin = {"run": "12345-1", "release_id": "published", "release_manifest_sha256": "a" * 64}
    if other == "wrong-run":
        origin["run"] = "54321-1"
    elif other == "wrong-attempt":
        origin["run"] = "12345-2"
    elif other == "wrong-lake":
        origin["release_id"] = "different"
    elif other == "wrong-digest":
        origin["release_manifest_sha256"] = "b" * 64
    remote.store_object("runs.sqlite", b"later coherent pair")
    _publish_receipt(remote, origin=origin if other else False)
    if other in {"wrong-lake", "wrong-digest"}:
        with pytest.raises(LakePublishError, match="receipt differs from the current lake release"):
            machine_bundle.read_published_machine(remote)
    else:
        # Ordinary reads do not require an original attempt or a daily stamp.
        machine_bundle.read_published_machine(remote)
    digest = hashlib.sha256(remote.values["machine-manifest.json"].body).hexdigest()
    monkeypatch.setattr(remote, "close", lambda: None, raising=False)
    monkeypatch.setattr(machine_bundle, "Boto3R2Store", lambda **_: remote)
    proof = tmp_path / "pair.json"
    assert (
        machine_bundle.main(
            ["--capture", str(proof), "--", "published", "a" * 64, "12345-1", digest]
        )
        == 1
    )
    assert not proof.exists()
    assert remote.put_keys == []


@pytest.mark.parametrize("run", ["", "12345", "12345-0", "12345-1\n", "other/12345-1"])
def test_invalid_original_attempt_stops_before_credentials(monkeypatch, run):
    monkeypatch.setattr(
        machine_bundle, "Boto3R2Store", lambda **_: pytest.fail("unexpected access")
    )
    assert machine_bundle.main(["--", "published", "a" * 64, run, "b" * 64]) == 1


@pytest.mark.parametrize(
    ("release", "digest"),
    [
        ("", "a" * 64),
        ("published", ""),
        ("../other", "a" * 64),
        ("--help", "a" * 64),
        ("published", "A" * 64),
        ("published", "a" * 63),
        ("published\n", "a" * 64),
    ],
)
def test_invalid_recovery_fails_without_opening_a_store(monkeypatch, release, digest):
    monkeypatch.setattr(
        machine_bundle, "Boto3R2Store", lambda **_: pytest.fail("unexpected access")
    )
    assert machine_bundle.main(["--", release, digest, "12345-1", "b" * 64]) == 1


@pytest.mark.parametrize("receipt", ["", "b" * 63, "B" * 64, "b" * 64 + "\n", "--help"])
def test_receipt_is_required_and_validated_before_opening_a_store(monkeypatch, receipt):
    monkeypatch.setattr(
        machine_bundle, "Boto3R2Store", lambda **_: pytest.fail("unexpected access")
    )
    assert machine_bundle.main(["--", "valid-1", "a" * 64, "12345-1", receipt]) == 1
    assert machine_bundle.main(["--", "valid-1", "a" * 64, "12345-1", "b" * 64]) == 0


@pytest.mark.parametrize("replacement", ["market.sqlite", "runs.sqlite", "receipt-bytes"])
def test_original_receipt_rejects_a_coherent_reseal_before_capture(
    tmp_path, monkeypatch, replacement
):
    remote = _remote_bundle()
    original = remote.values["machine-manifest.json"].body
    digest = hashlib.sha256(original).hexdigest()
    if replacement == "receipt-bytes":
        remote.store_object("machine-manifest.json", original + b"\n")
    else:
        remote.store_object(replacement, b"later bytes under the same lake")
        _publish_receipt(remote)
    # Both store heads still agree with the new receipt, as in a later successful push.
    machine_bundle.read_published_machine(remote)
    remote.get_keys.clear()
    remote.head_keys.clear()
    monkeypatch.setattr(remote, "close", lambda: None, raising=False)
    monkeypatch.setattr(machine_bundle, "Boto3R2Store", lambda **_: remote)
    proof = tmp_path / "pair.json"
    assert (
        machine_bundle.main(
            ["--capture", str(proof), "--", "published", "a" * 64, "12345-1", digest]
        )
        == 1
    )
    assert not proof.exists()
    assert remote.put_keys == []
    assert remote.get_keys.count("machine-manifest.json") == 1
    assert not {"market.sqlite", "runs.sqlite"} & set(remote.get_keys + remote.head_keys)


def test_matching_original_receipt_adds_no_remote_calls():
    remote = _remote_bundle()
    digest = hashlib.sha256(remote.values["machine-manifest.json"].body).hexdigest()
    original = machine_bundle.read_published_machine(remote)
    calls = (remote.get_keys[:], remote.head_keys[:])
    remote.get_keys.clear()
    remote.head_keys.clear()
    assert machine_bundle.read_published_machine(remote, expected_receipt_sha256=digest) == original
    assert (remote.get_keys, remote.head_keys) == calls
    assert remote.get_keys.count("machine-manifest.json") == 1
    assert remote.put_keys == []


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "missing-pointer",
        "new-release",
        "new-digest",
        "missing-receipt",
        "missing-runs",
        "partial-market",
        "partial-runs",
        "resealed-pair",
        "missing-origin",
        "other-origin",
    ],
)
def test_display_guard_accepts_only_the_same_published_pair_and_local_origin(
    tmp_path, monkeypatch, change
):
    remote = _remote_bundle()
    monkeypatch.setattr(remote, "close", lambda: None, raising=False)
    monkeypatch.setattr(machine_bundle, "Boto3R2Store", lambda **_: remote)
    market = tmp_path / "market.sqlite"
    monkeypatch.setattr(machine_bundle, "MARKET_DB_PATH", market)
    proof = str(tmp_path / "pair.json")
    target = [
        "--",
        "published",
        "a" * 64,
        "12345-1",
        hashlib.sha256(remote.values["machine-manifest.json"].body).hexdigest(),
    ]
    assert machine_bundle.main(["--capture", proof, *target]) == 0
    with closing(open_connection(market)) as conn, conn:
        if change != "missing-origin":
            write_lake_store_origin(
                conn,
                LakeStoreOrigin("older" if change == "other-origin" else "published", "a" * 64),
            )
    pointer = current_release_pointer_key()
    if change in {"new-release", "new-digest"}:
        release = "new" if change == "new-release" else "published"
        remote.store_object(
            pointer,
            json.dumps(
                {
                    "release_id": release,
                    "manifest_key": release_manifest_key(release_id=release),
                    "manifest_sha256": ("b" if change == "new-digest" else "a") * 64,
                }
            ).encode(),
        )
    elif change.startswith("missing-") and change != "missing-origin":
        del remote.values[
            {
                "missing-pointer": pointer,
                "missing-receipt": "machine-manifest.json",
                "missing-runs": "runs.sqlite",
            }[change]
        ]
    elif change in {"partial-market", "partial-runs", "resealed-pair"}:
        remote.store_object(
            "market.sqlite" if change == "partial-market" else "runs.sqlite", b"changed"
        )
        if change == "resealed-pair":
            _publish_receipt(remote)
    assert machine_bundle.main(["--verify", proof, *target]) == (0 if change == "none" else 1)
    assert remote.put_keys == []
    assert set(remote.get_keys) <= {pointer, "machine-manifest.json"}
    assert set(remote.head_keys) <= {pointer, "market.sqlite", "runs.sqlite"}
