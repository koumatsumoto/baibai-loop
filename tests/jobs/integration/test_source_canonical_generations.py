"""Scoped inputs bind to the published machine bundle, not a pulled CAS base."""

import json
from types import SimpleNamespace

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.integration import sources

MACHINE_KEYS = ("market.sqlite", "runs.sqlite", "indicators.sqlite")


def _remote_bundle() -> MemoryR2Store:
    remote = MemoryR2Store()
    for key in MACHINE_KEYS:
        remote.store_object(key, f"initial {key}".encode())
    _publish_receipt(remote)
    return remote


def _publish_receipt(remote: MemoryR2Store) -> None:
    receipt = {
        "schema_version": 1,
        "keys": {key: remote.values[key].etag for key in MACHINE_KEYS},
    }
    remote.store_object("machine-manifest.json", json.dumps(receipt).encode())


def _fixed_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sources,
        "_serving_pointer",
        lambda _: SimpleNamespace(
            pointer=SimpleNamespace(release_id="published", manifest_sha256="a" * 64)
        ),
    )


@pytest.mark.parametrize("changed_key", list(MACHINE_KEYS))
def test_published_generation_refuses_any_partial_machine_push(
    tmp_path, monkeypatch: pytest.MonkeyPatch, changed_key: str
) -> None:
    _fixed_pointer(monkeypatch)
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
        "economic_etag": remote.values["indicators.sqlite"].etag,
    }
    assert sidecar.read_text() == old_economic


def test_inputs_cli_stops_before_publishing_when_machine_receipt_is_stale(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixed_pointer(monkeypatch)
    remote = _remote_bundle()
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: remote)
    monkeypatch.setattr(sources, "_load_handle", lambda _: object())
    monkeypatch.setattr(sources, "verify_held", lambda *_: None)
    published = []

    def publish(*_args, **kwargs):
        published.append(kwargs["generations"])
        return {"updated": ["economic-fred_csv"], "failed": []}

    monkeypatch.setattr(sources, "publish_inputs", publish)
    pulled_economic = remote.values["indicators.sqlite"].etag
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
        "economic-fred_csv",
    ]
    assert sources.main(command) == 0
    assert published[0]["economic_etag"] == remote.values["indicators.sqlite"].etag
    assert published[0]["economic_etag"] != pulled_economic

    remote.store_object("runs.sqlite", b"partial machine upload")
    assert sources.main(command) == 1
    assert len(published) == 1
