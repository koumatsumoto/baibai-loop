from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import pytest

from baibai_batch.storage.lake_publish import (
    Boto3R2Store,
    LakeCASConflict,
    LakePublishError,
    RemoteObject,
    publish_l1_release,
)
from baibai_engine.market.lake import models as lake_models
from baibai_engine.market.lake.datasets import LAKE_DATASETS
from baibai_engine.market.lake.hydrate import hydrate_market_store
from baibai_engine.market.lake.keys import (
    current_l1_pointer_key,
    dataset_manifest_key,
)
from baibai_engine.market.lake.models import (
    ReleaseManifest,
    canonical_lake_model_bytes,
    load_lake_model_json,
)
from baibai_engine.market.lake.objects import open_lake
from baibai_engine.market.lake.prefetch import prefetching_hydration_cache
from baibai_engine.market.lake.reader import resolve_current_release
from baibai_engine.market.lake.release import L1ReleasePointer, create_l1_release
from baibai_engine.market.lake.writer import export_lake_legacy
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


def _emptied_market(source: Path, destination: Path) -> Path:
    """A copy of the market store shaped like the object R2 holds: no lake-owned row."""

    destination.write_bytes(source.read_bytes())
    connection = sqlite3.connect(destination)
    for dataset in LAKE_DATASETS.values():
        connection.execute(f"DELETE FROM {dataset.sqlite_table}")  # nosec B608
    connection.commit()
    connection.close()
    return destination


def _lake_table_contents(path: Path) -> dict[str, list[tuple[object, ...]]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {
            dataset.sqlite_table: [
                tuple(row)
                for row in connection.execute(
                    f"SELECT * FROM {dataset.sqlite_table} "  # nosec B608
                    f"ORDER BY {', '.join(dataset.primary_key)}"
                )
            ]
            for dataset in sorted(LAKE_DATASETS.values(), key=lambda item: item.sqlite_table)
        }
    finally:
        connection.close()


def _store() -> Boto3R2Store:
    bucket = os.environ["R2_LAKE_ACCEPTANCE_BUCKET"]
    if bucket == "baibai-stores" or "acceptance" not in bucket:
        pytest.fail("R2 acceptance must use a dedicated non-production bucket")
    return Boto3R2Store(bucket=bucket)


def _allow_tiny_pilot(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = lake_models.PRODUCTION_RELEASE_POLICY
    monkeypatch.setattr(
        lake_models,
        "PRODUCTION_RELEASE_POLICY",
        policy.model_copy(
            update={
                "datasets": tuple(
                    item.model_copy(
                        update={
                            "coverage_start_on_or_before": date.max,
                            "minimum_rows": 1,
                            "minimum_population_count": 1,
                            "max_age_days": 10_000,
                            "max_lead_days": 10_000,
                        }
                    )
                    for item in policy.datasets
                    if item.dataset in {"jquants.daily_bars", "jquants.short_sale_reports"}
                ),
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


def _download_l1_closure(store: Boto3R2Store, mirror: Path) -> None:
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


def test_actual_r2_l1_publish_and_read_back_into_a_market_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_tiny_pilot(monkeypatch)
    store = _store()
    mirror = tmp_path / "publisher"
    sqlite_path = _tiny_market(tmp_path / "market.sqlite")
    build = export_lake_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        expected_store_origin=None,
    )
    release_path, _release = create_l1_release(
        dataset_manifest_paths=[item.manifest_path for item in build.datasets.values()],
        mirror_root=mirror,
        release_id=f"acceptance-{uuid.uuid4().hex}",
        created_at=datetime.now(UTC),
    )

    first = publish_l1_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
    immediate = store.head(current_l1_pointer_key())
    assert immediate is not None
    assert immediate.etag == first.pointer_etag
    immediate_pointer = load_lake_model_json(
        store.get_bytes(current_l1_pointer_key()), L1ReleasePointer
    )
    assert immediate_pointer.release_id == first.release_id
    remote_mirror = tmp_path / "remote-reader"
    _download_l1_closure(store, remote_mirror)

    mirrored_store = _emptied_market(sqlite_path, tmp_path / "mirrored.sqlite")
    with open_lake(mirror=remote_mirror) as (session, cache):
        fixed = resolve_current_release(cache.source, evaluated_at=datetime.now(UTC))
        mirrored = hydrate_market_store(
            session,
            release=fixed,
            cache=cache,
            store=mirrored_store,
            dataset_names=tuple(sorted(build.datasets)),
        )
    assert mirrored.release_id == fixed.release_id
    with sqlite3.connect(mirrored_store) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)

    # The reader path production uses: objects resolved out of the bucket through
    # DuckDB's HTTP layer, prefetched by the same worker wiring `lake hydrate` runs,
    # with nothing pre-downloaded beside it.
    direct_mirror = tmp_path / "direct-reader"
    direct_store = _emptied_market(sqlite_path, tmp_path / "direct.sqlite")
    with open_lake(mirror=direct_mirror, bucket=os.environ["R2_LAKE_ACCEPTANCE_BUCKET"]) as (
        session,
        cache,
    ):
        direct = resolve_current_release(cache.source, evaluated_at=datetime.now(UTC))
        with prefetching_hydration_cache(
            cache,
            release=direct,
            dataset_names=tuple(sorted(build.datasets)),
            bucket=os.environ["R2_LAKE_ACCEPTANCE_BUCKET"],
        ) as hydration_cache:
            hydrate_market_store(
                session,
                release=direct,
                cache=hydration_cache,
                store=direct_store,
                dataset_names=tuple(sorted(build.datasets)),
            )
    # Reading the same release over HTTP and out of a downloaded mirror has to produce
    # the same rows; comparing the filled tables is what proves it rather than assuming.
    assert _lake_table_contents(direct_store) == _lake_table_contents(mirrored_store)
    assert _lake_table_contents(mirrored_store) == _lake_table_contents(sqlite_path)

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


@pytest.mark.parametrize("fault", ["put", "head", "get", "conflict", "unknown"])
def test_actual_r2_reconciles_a_real_pointer_commit_after_a_wrapper_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: Literal["put", "head", "get", "conflict", "unknown"],
) -> None:
    """Keep R2 responsible for the commit while a thin wrapper loses its result."""

    _allow_tiny_pilot(monkeypatch)
    delegate = _store()
    mirror = tmp_path / "publisher"
    sqlite_path = _tiny_market(tmp_path / "market.sqlite")
    build = export_lake_legacy(
        sqlite_path=sqlite_path,
        mirror_root=mirror,
        producer_git_commit="b" * 40,
        expected_store_origin=None,
    )
    release_id = f"acceptance-ambiguous-{fault}-{uuid.uuid4().hex}"
    release_path, _release = create_l1_release(
        dataset_manifest_paths=[item.manifest_path for item in build.datasets.values()],
        mirror_root=mirror,
        release_id=release_id,
        created_at=datetime.now(UTC),
    )
    pointer_key = current_l1_pointer_key()

    class FaultAfterPointerCommit:
        def __init__(self) -> None:
            self.armed = False

        def head(self, key: str) -> RemoteObject | None:
            if self.armed and key == pointer_key:
                if fault == "head":
                    self.armed = False
                    raise LakePublishError("injected one-shot HEAD failure after real commit")
                if fault == "unknown":
                    raise LakePublishError("injected unreadable pointer after real commit")
            return delegate.head(key)

        def get_bytes(self, key: str) -> bytes:
            if self.armed and key == pointer_key and fault == "get":
                self.armed = False
                raise LakePublishError("injected one-shot GET failure after real commit")
            return delegate.get_bytes(key)

        def download_file(self, key: str, path: Path, *, expect_bytes: int | None = None) -> None:
            delegate.download_file(key, path, expect_bytes=expect_bytes)

        def put_file(
            self,
            key: str,
            path: Path,
            *,
            sha256: str,
            content_md5: str,
            content_type: str,
            if_match: str | None = None,
            if_none_match: bool = False,
        ) -> RemoteObject:
            result = delegate.put_file(
                key,
                path,
                sha256=sha256,
                content_md5=content_md5,
                content_type=content_type,
                if_match=if_match,
                if_none_match=if_none_match,
            )
            if key != pointer_key:
                return result
            self.armed = True
            if fault == "conflict":
                successor = canonical_lake_model_bytes(
                    L1ReleasePointer(
                        release_id=f"acceptance-successor-{uuid.uuid4().hex}",
                        manifest_key=("lake/manifests/releases/l1/acceptance-successor.json"),
                        manifest_sha256="f" * 64,
                    )
                )
                successor_path = tmp_path / "successor-pointer.json"
                successor_path.write_bytes(successor)
                delegate.put_file(
                    key,
                    successor_path,
                    sha256=hashlib.sha256(successor).hexdigest(),
                    content_md5=_content_md5(successor_path),
                    content_type="application/json",
                    if_match=result.etag,
                )
                raise LakePublishError("injected error after a real successor won")
            if fault in {"put", "unknown"}:
                raise LakePublishError("injected response loss after real pointer commit")
            return result

    store = FaultAfterPointerCommit()
    if fault == "conflict":
        with pytest.raises(LakeCASConflict, match="different release"):
            publish_l1_release(
                mirror_root=mirror,
                release_manifest_path=release_path,
                store=store,
            )
        return
    if fault == "unknown":
        with pytest.raises(LakePublishError, match="outcome is unknown"):
            publish_l1_release(
                mirror_root=mirror,
                release_manifest_path=release_path,
                store=store,
            )
        return

    report = publish_l1_release(
        mirror_root=mirror,
        release_manifest_path=release_path,
        store=store,
    )
    assert report.release_id == release_id
    current = load_lake_model_json(delegate.get_bytes(pointer_key), L1ReleasePointer)
    assert current.release_id == release_id


def test_actual_r2_wrong_credentials_do_not_leak(tmp_path: Path) -> None:
    secret = f"acceptance-secret-{uuid.uuid4().hex}"
    store = Boto3R2Store(
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
