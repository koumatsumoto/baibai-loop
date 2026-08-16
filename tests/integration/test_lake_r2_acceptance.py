from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from baibai_batch.storage.lake_publish import (
    AwsCliR2Store,
    LakeCASConflict,
    LakePublishError,
    RemoteObject,
    _ensure_immutable,
    _RemotePublication,
    publish_l1_release,
)
from baibai_engine.market.lake import models as lake_models
from baibai_engine.market.lake.datasets import PILOT_DATASETS
from baibai_engine.market.lake.keys import (
    current_l1_pointer_key,
    dataset_manifest_key,
)
from baibai_engine.market.lake.models import (
    ReleaseManifest,
    load_lake_model_json,
)
from baibai_engine.market.lake.objects import open_lake, sha256_file
from baibai_engine.market.lake.projection import build_projection
from baibai_engine.market.lake.reader import resolve_current_release
from baibai_engine.market.lake.release import L1ReleasePointer, create_l1_release
from baibai_engine.market.lake.writer import export_pilot_legacy
from baibai_engine.market.sqlite import open_connection

pytestmark = pytest.mark.skipif(
    os.environ.get("BAIBAI_R2_ACCEPTANCE") != "1",
    reason="dedicated R2 acceptance credentials are not configured",
)


def _content_md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)  # nosec B324
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode()


def _store() -> AwsCliR2Store:
    bucket = os.environ["R2_LAKE_ACCEPTANCE_BUCKET"]
    if bucket == "baibai-stores" or "acceptance" not in bucket:
        pytest.fail("R2 acceptance must use a dedicated non-production bucket")
    return AwsCliR2Store(bucket=bucket)


def _allow_tiny_pilot(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = lake_models.PILOT_RELEASE_POLICY
    monkeypatch.setattr(
        lake_models,
        "PILOT_RELEASE_POLICY",
        policy.model_copy(
            update={
                "datasets": tuple(
                    item.model_copy(
                        update={
                            "coverage_start_on_or_before": date.max,
                            "minimum_rows": 1,
                            "minimum_population_count": 1,
                        }
                    )
                    for item in policy.datasets
                ),
                "max_dataset_age_days": 10_000,
                "max_dataset_skew_days": 10_000,
            }
        ),
    )


def _tiny_market(path: Path) -> Path:
    connection = open_connection(path)
    connection.execute(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close, volume) "
        "VALUES ('1301', '2026-01-05', 100.0, 1000.0)"
    )
    connection.execute(
        """INSERT INTO jquants_short_sale_reports(
             disclosed_at, source_ordinal, calculated_at, ticker, short_seller_name,
             discretionary_investment_contractor_name, investment_fund_name,
             short_ratio, short_shares, short_trading_units, is_cancellation
           ) VALUES ('2026-01-06', 0, '2026-01-05', '1301', 'Fund', '', '',
                     0.006, 600, 6, 0)"""
    )
    connection.execute(
        """INSERT INTO source_coverage(
             source, coverage_key, coverage_start, coverage_end,
             fetched_at_utc, record_count, status, error
           ) VALUES ('jquants_short_sale_reports', 'acceptance', '2026-01-01',
                     '2026-01-31', '2026-02-01T00:00:00+00:00', 1, 'ok', NULL)"""
    )
    connection.commit()
    connection.close()
    return path


def _download_l1_closure(store: AwsCliR2Store, mirror: Path) -> None:
    def download(key: str) -> bytes:
        path = mirror / key
        path.parent.mkdir(parents=True, exist_ok=True)
        store.download_file(key, path)
        return path.read_bytes()

    pointer = load_lake_model_json(download(current_l1_pointer_key()), L1ReleasePointer)
    release = load_lake_model_json(download(pointer.manifest_key), ReleaseManifest)
    for name, reference in release.datasets.items():
        key = dataset_manifest_key(dataset=name, build_id=reference.build_id)
        manifest = load_lake_model_json(download(key), lake_models.DatasetManifest)
        for partition in manifest.partitions:
            for item in partition.objects:
                download(item.key)


def test_actual_r2_conditional_writes_refuse_a_stale_generation(tmp_path: Path) -> None:
    """Conditional writes are the only thing standing between two writers and a lost update.

    Both pointers move under `--if-match` against the ETag the publisher read, or
    `--if-none-match` when it read nothing, and rollback no longer exists to undo a
    pointer that the wrong writer won. Two things can only be observed against a real
    bucket: that R2 enforces the precondition at all rather than accepting the write,
    and that the CLI's refusal is recognised as a conflict rather than a generic
    failure. Neither is visible to a fake store, which raises the conflict by
    construction. A throwaway key carries the same object semantics as a pointer key
    without making one run's serving state depend on another's.
    """

    store = _store()
    key = f"lake/acceptance/conditional-write/{uuid.uuid4().hex}.json"

    def write(
        generation: str, *, if_match: str | None = None, if_none_match: bool = False
    ) -> RemoteObject:
        payload = json.dumps({"generation": generation}, separators=(",", ":")).encode() + b"\n"
        path = tmp_path / f"{generation}.json"
        path.write_bytes(payload)
        return store.put_file(
            key,
            path,
            sha256=hashlib.sha256(payload).hexdigest(),
            content_md5=_content_md5(path),
            content_type="application/json",
            if_match=if_match,
            if_none_match=if_none_match,
        )

    first = write("first", if_none_match=True)
    # The ETag travels back through head-object, which strips the quotes R2 sends; a
    # precondition that only ever refuses would pass this test's negative half while
    # blocking every real publication, so the accepted write is the half that matters.
    second = write("second", if_match=first.etag)

    with pytest.raises(LakeCASConflict):
        write("stale", if_match=first.etag)
    with pytest.raises(LakeCASConflict):
        write("recreated", if_none_match=True)

    served = store.head(key)
    assert served is not None
    assert served.etag == second.etag
    assert json.loads(store.get_bytes(key))["generation"] == "second"


def test_actual_r2_l1_publish_read_and_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_tiny_pilot(monkeypatch)
    store = _store()
    mirror = tmp_path / "publisher"
    sqlite_path = _tiny_market(tmp_path / "market.sqlite")
    build = export_pilot_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
    )
    release_path, _release = create_l1_release(
        dataset_manifest_paths=[item.manifest_path for item in build.datasets.values()],
        mirror_root=mirror,
        release_id=f"acceptance-{uuid.uuid4().hex}",
        created_at=datetime.now(UTC),
    )

    first = publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
    remote_mirror = tmp_path / "remote-reader"
    _download_l1_closure(store, remote_mirror)

    with open_lake(mirror=remote_mirror) as (session, cache):
        fixed = resolve_current_release(cache.source, evaluated_at=datetime.now(UTC))
        projection = build_projection(
            session,
            release=fixed,
            cache=cache,
            destination=tmp_path / "projection.sqlite",
            dataset_names=tuple(sorted(PILOT_DATASETS)),
            builder_git_commit="a" * 40,
        )
    assert projection.identity.source_release_id == fixed.release_id
    with sqlite3.connect(projection.path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)

    # The reader path production would use: objects resolved straight out of the bucket
    # through DuckDB's HTTP layer, with nothing pre-downloaded beside it.
    direct_mirror = tmp_path / "direct-reader"
    with open_lake(mirror=direct_mirror, bucket=os.environ["R2_LAKE_ACCEPTANCE_BUCKET"]) as (
        session,
        cache,
    ):
        direct = resolve_current_release(cache.source, evaluated_at=datetime.now(UTC))
        direct_projection = build_projection(
            session,
            release=direct,
            cache=cache,
            destination=tmp_path / "direct-projection.sqlite",
            dataset_names=tuple(sorted(PILOT_DATASETS)),
            builder_git_commit="a" * 40,
        )
    assert direct_projection.identity == projection.identity

    # A run that changes nothing must not move the closure's bytes again.
    unchanged = publish_l1_release(
        mirror_root=mirror, release_manifest_path=release_path, store=store
    )
    assert unchanged.transfers.uploaded_bytes == 0
    assert unchanged.transfers.downloaded_bytes < first.transfers.uploaded_bytes

    # Publishing forward moves current and nothing else: the pointer names exactly the
    # generation just published, and repair is another publication rather than a step
    # backwards.
    successor_id = f"acceptance-{uuid.uuid4().hex}"
    successor_path, _successor = create_l1_release(
        dataset_manifest_paths=[item.manifest_path for item in build.datasets.values()],
        mirror_root=mirror,
        release_id=successor_id,
        created_at=datetime.now(UTC),
    )
    publish_l1_release(mirror_root=mirror, release_manifest_path=successor_path, store=store)
    serving = load_lake_model_json(store.get_bytes(current_l1_pointer_key()), L1ReleasePointer)
    assert serving.release_id == successor_id


def test_actual_r2_large_reuse_reads_bytes_and_rejects_forged_metadata(tmp_path: Path) -> None:
    store = _store()
    size = 2 * 1024 * 1024 * 1024
    payload = tmp_path / "two-gib.bin"
    with payload.open("wb") as handle:
        handle.truncate(size)
    digest = sha256_file(payload)
    key = f"lake/acceptance/large/{digest}.bin"
    if store.head(key) is not None:
        store.put_file(
            key,
            payload,
            sha256=digest,
            content_md5=_content_md5(payload),
            content_type="application/octet-stream",
        )
    started = time.perf_counter()
    _ensure_immutable(
        _RemotePublication(store=store),
        key=key,
        path=payload,
        content_type="application/octet-stream",
        expected_sha256=digest,
        expected_size=size,
    )
    first_seconds = time.perf_counter() - started

    forged = tmp_path / "forged.bin"
    with forged.open("wb") as handle:
        handle.write(b"forged")
        handle.truncate(size)
    store.put_file(
        key,
        forged,
        sha256=digest,
        content_md5=_content_md5(forged),
        content_type="application/octet-stream",
    )
    # Forged bytes under an unchanged identity are what the streaming audit exists for;
    # the publication path proves the identity metadata and leaves the stream to it.
    _ensure_immutable(
        _RemotePublication(store=store),
        key=key,
        path=payload,
        content_type="application/octet-stream",
        expected_sha256=digest,
        expected_size=size,
    )
    with pytest.raises(LakePublishError, match="bytes postcondition failed"):
        _ensure_immutable(
            _RemotePublication(store=store, verify_bytes=True),
            key=key,
            path=payload,
            content_type="application/octet-stream",
            expected_sha256=digest,
            expected_size=size,
        )
    store.put_file(
        key,
        payload,
        sha256=digest,
        content_md5=_content_md5(payload),
        content_type="application/octet-stream",
    )
    print(json.dumps({"large_object_bytes": size, "first_publish_seconds": first_seconds}))


def test_actual_r2_wrong_credentials_do_not_leak(tmp_path: Path) -> None:
    secret = f"acceptance-secret-{uuid.uuid4().hex}"
    store = AwsCliR2Store(
        bucket=os.environ["R2_LAKE_ACCEPTANCE_BUCKET"],
        env={
            "PATH": os.environ["PATH"],
            "R2_ACCOUNT_ID": os.environ["R2_ACCOUNT_ID"],
            "R2_ACCESS_KEY_ID": "wrong-access-key",
            "R2_SECRET_ACCESS_KEY": secret,
        },
    )
    with pytest.raises(LakePublishError) as caught:
        store.head(f"lake/acceptance/wrong-credential/{tmp_path.name}")
    assert secret not in str(caught.value)
