"""Retiring articles from daily delivery does not retire fixed article evidence."""

import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from types import SimpleNamespace

from tests.helpers.jquants_receipts import receive
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import export_market_fixture, market_store
from tests.helpers.master_calendar_native import seed_calendar, seed_master
from tests.helpers.price_native import seed_prices
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.trends import PHARMA

from baibai_engine.data_collection.receipts.articles.contracts import (
    ArticleEvidenceRef,
    ArticleReceipt,
    BodyRef,
)
from baibai_engine.data_collection.receipts.articles.store import retain_receipt
from baibai_engine.machine_analysis.observations.articles.projection import project_receipt
from baibai_engine.machine_analysis.observations.articles.reader import FixedArticleReader
from baibai_engine.machine_analysis.observations.trends.extraction import extract_observations
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.hydrate import dehydrate_market_store
from baibai_engine.storage.snapshots.objects import LakeObjectCache
from baibai_engine.storage.snapshots.reader import resolve_current_release, resolve_release
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    write_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.publication import snapshot_publication as publication
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile
from baibai_jobs.publication.snapshot_release import publish_release
from baibai_jobs.publication.snapshot_write_cli import hydrate_release

ARCHIVE = {
    "articles.source_receipts",
    "articles.projection_runs",
    "articles.observations",
    "articles.extraction_artifacts",
}
ACTIVE = "jquants.source_pages"
DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 2, tzinfo=UTC)


def test_daily_retires_article_payloads_but_fixed_evidence_stays_readable(tmp_path, monkeypatch):
    catalog = {name: LAKE_DATASETS[name] for name in sorted(ARCHIVE | {ACTIVE})}
    narrow_release_policy(monkeypatch, datasets=catalog, carries_history=None)
    monkeypatch.setattr(publication, "LAKE_DATASETS", catalog)
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)

    remote = MemoryR2Store()
    source = SimpleNamespace(read_bytes=remote.get_bytes)
    mirror = tmp_path / "publish"
    market = market_store(
        tmp_path / "market.sqlite",
        daily_bars=(("1301", DAY.isoformat(), 100.0, 1000.0),),
        short_sale_reports=(),
    )
    seed_prices(remote, market, tmp_path / "price-source")
    seed_calendar(remote, market, tmp_path / "calendar-source", as_of=DAY)
    seed_master(remote, market, tmp_path / "master-source", as_of=DAY)
    request = receive(market, "margin_alerts", {}, b'{"data":[]}', AT)
    body = f'<div class="entry"><h1>合成・医薬品調査</h1><p>{PHARMA}</p></div>'.encode()
    body_ref = BodyRef(receipt_id="retained-original", sha256=hashlib.sha256(body).hexdigest())
    receipt = ArticleReceipt(
        receipt_id=body_ref.receipt_id,
        source="tsr_data",
        requested_url="https://example.invalid/synthetic-pharma-survey",
        received_at=AT,
        status="ok",
        http_status=200,
        encoding="utf-8",
        body_ref=body_ref,
    )
    with closing(sqlite3.connect(market)) as connection, connection:
        retain_receipt(connection, receipt, body=body)
        _, (observation,) = project_receipt(
            connection, receipt.receipt_id, kind="document", generated_at=AT
        )
        (artifact,) = extract_observations(
            connection, [observation.observation_id], generated_at=AT
        )
    export = export_market_fixture(
        sqlite_path=market,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        catalog=catalog,
        expected_store_origin=None,
        created_at=AT,
    )
    assert set(export.datasets) == ARCHIVE | {ACTIVE}
    old_path, _ = create_release(
        dataset_manifest_paths=[item.manifest_path for item in export.datasets.values()],
        mirror_root=mirror,
        release_id="with-articles",
        catalog=catalog,
        policy=release_policy_for_profile("production"),
    )
    publish_release(mirror_root=mirror, release_manifest_path=old_path, store=remote)
    old = resolve_current_release(
        source, catalog=catalog, policy=release_policy_for_profile("production")
    )
    article_keys = {
        item.key
        for name in ARCHIVE
        for part in old.dataset_manifests[name].partitions
        for item in part.objects
    }
    assert len(article_keys) == len(ARCHIVE)
    old_article_bodies = {key: remote.values[key].body for key in article_keys}
    with closing(sqlite3.connect(market)) as connection, connection:
        write_lake_store_origin(connection, LakeStoreOrigin(old.release_id, old.manifest_sha256))
    dehydrate_market_store(market, catalog=catalog, release=old)

    remote.get_keys.clear()
    cache = LakeObjectCache(tmp_path / "daily-fetch", source)
    with lake_session() as session:
        daily = hydrate_release(
            session, cache, store=market, release=old, bucket=None, mode="daily"
        )
    assert daily.rows == {ACTIVE: 1}
    assert article_keys.isdisjoint(remote.get_keys)
    with closing(sqlite3.connect(market)) as connection:
        for name in ARCHIVE:
            assert connection.execute(
                f"SELECT COUNT(*) FROM {catalog[name].sqlite_table}"  # nosec B608
            ).fetchone() == (0,)
        assert connection.execute("SELECT request_id FROM jquants_source_pages").fetchone() == (
            request,
        )

    remote.put_keys.clear()
    with publication.prepare_market_lake(
        sqlite_path=market, mirror_root=mirror, store=remote, as_of=DAY, release_id="current"
    ) as prepared:
        assert set(prepared.release.datasets) == set(prepared.export.datasets) == {ACTIVE}
        publication.publish_prepared_market_lake(prepared, store=remote)
    assert article_keys.isdisjoint(remote.get_keys + remote.put_keys)
    assert remote.delete_keys == []
    assert old_article_bodies == {key: remote.values[key].body for key in article_keys}

    current = resolve_current_release(
        source, catalog=catalog, policy=release_policy_for_profile("production")
    )
    assert current.release_id == "current"
    dehydrate_market_store(market, catalog=catalog, release=current)
    with lake_session() as session:
        tomorrow = hydrate_release(
            session, cache, store=market, release=current, bucket=None, mode="daily"
        )
    assert tomorrow.rows == {ACTIVE: 1}
    assert article_keys.isdisjoint(remote.get_keys)

    # The same archived graphs still resolve by their explicit old ID and digest.
    archived = resolve_release(source, old.release_id, manifest_sha256=old.manifest_sha256)
    historical = tmp_path / "historical.sqlite"
    open_connection(historical).close()
    with lake_session() as session:
        full = hydrate_release(
            session, cache, store=historical, release=archived, bucket=None, mode="full"
        )
    assert full.rows == dict.fromkeys(ARCHIVE | {ACTIVE}, 1)
    evidence = FixedArticleReader(archived, cache).exact_evidence(
        ArticleEvidenceRef(
            lineage_release_id=old.release_id,
            lineage_manifest_sha256=old.manifest_sha256,
            receipt_id=receipt.receipt_id,
            receipt_sha256=receipt.sha256,
            body_ref=body_ref,
            original_url=receipt.requested_url,
            observation_id=observation.observation_id,
            observation_sha256=observation.sha256,
            artifact_id=artifact.artifact_id,
            artifact_sha256=artifact.sha256,
        )
    )
    assert evidence.body == body
    assert evidence.artifact == artifact
