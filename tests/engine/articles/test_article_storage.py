"""Synthetic originals only: receipt closure, immutable artifacts and retained exact evidence."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from baibai_engine.data_collection.receipts.articles.contracts import (
    ArticleEvidenceRef,
    ArticleReceipt,
    BodyRef,
    digest_json,
)
from baibai_engine.data_collection.receipts.articles.snapshot_contracts import ARTICLE_RECEIPTS
from baibai_engine.data_collection.receipts.articles.storage_contracts import ARTICLE_RECEIPTS_DDL
from baibai_engine.data_collection.receipts.articles.store import (
    ArticleStoreError,
    copy_receipt_closure,
    read_body,
    read_receipt,
    retain_receipt,
)
from baibai_engine.machine_analysis.observations.articles.contracts import ArticleExtractionArtifact
from baibai_engine.machine_analysis.observations.articles.projection import project_receipt
from baibai_engine.machine_analysis.observations.articles.reader import FixedArticleReader
from baibai_engine.machine_analysis.observations.articles.snapshot_contracts import (
    ARTICLE_EXTRACTION_ARTIFACTS,
    ARTICLE_OBSERVATIONS,
    ARTICLE_PROJECTION_RUNS,
)
from baibai_engine.machine_analysis.observations.articles.storage_contracts import (
    ARTICLE_OBSERVATIONS_DDL,
)
from baibai_engine.machine_analysis.observations.articles.store import (
    read_artifacts,
    read_observations,
    save_artifact,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.snapshots.models import ReleaseDatasetPolicy, ReleasePolicy
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache import schema
from baibai_engine.storage.sqlite.source_cache.article_upgrade import upgrade_article_working_copy

AT = datetime(2026, 10, 1, tzinfo=UTC)
URL = "https://www.tsr-net.co.jp/data/json/search-2.json"
CATALOG = {
    d.name: d
    for d in (
        ARTICLE_RECEIPTS,
        ARTICLE_OBSERVATIONS,
        ARTICLE_PROJECTION_RUNS,
        ARTICLE_EXTRACTION_ARTIFACTS,
    )
}


def test_article_digest_reuses_canonical_json_without_changing_json_value_hashes():
    value = {"z": [True, None, 3.25], "a": {"日本語": "合成"}}
    original = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    assert canonical_json(value).encode("utf-8") == original
    assert digest_json(value) == hashlib.sha256(original).hexdigest()


def test_exact_304_body_owner_can_be_in_an_earlier_month(tmp_path):
    first_at = datetime(2026, 9, 30, 23, 59, tzinfo=UTC)
    second_at = datetime(2026, 10, 1, 0, 1, tzinfo=UTC)
    raw = payload()
    first = receipt("september", raw, at=first_at)
    unchanged = ArticleReceipt(
        receipt_id="october-304",
        source="tsr_data",
        requested_url=URL,
        received_at=second_at,
        status="not_modified",
        http_status=304,
        body_ref=first.body_ref,
    )
    path = tmp_path / "market.sqlite"
    with closing(schema.open_connection(path)) as conn, conn:
        retain_receipt(conn, first, body=raw)
        retain_receipt(conn, unchanged)
        project_receipt(conn, unchanged.receipt_id, generated_at=second_at)
    reader = publish(path, tmp_path / "mirror", "cross-month", second_at)
    listing = reader.list(month="2026-10")
    reference = ArticleEvidenceRef.model_validate(listing["items"][0]["reference"])
    assert reference.receipt_month == "2026-10"
    assert reference.body_month == "2026-09"
    assert reader.get(reference).body == raw


def receipt(identity, body, *, at=AT):
    return ArticleReceipt(
        receipt_id=identity,
        source="tsr_data",
        requested_url=URL,
        received_at=at,
        status="ok",
        http_status=200,
        etag='"synthetic"',
        body_ref=BodyRef(receipt_id=identity, sha256=hashlib.sha256(body).hexdigest()),
    )


def payload(summary="合成調査の対象は12社。", *, title="合成業界調査"):
    return json.dumps(
        [
            {
                "url": "/data/detail/synthetic.html",
                "title": title,
                "list_text": summary,
                "viewing_start_ymd": "2026-09-30 15:00:00",
            }
        ],
        ensure_ascii=False,
    ).encode()


def artifact(observation, *, revision="synthetic-v1", status="extracted"):
    return ArticleExtractionArtifact(
        observation_id=observation.observation_id,
        observation_sha256=observation.sha256,
        text_sha256=observation.text_sha256,
        text_revision=observation.text_revision,
        generated_at=AT,
        method="parser",
        extractor_revision=revision,
        status=status,
        payload={"claims": [{"kind": "reported_fact", "value": 12, "unit": "companies"}]},
    )


def publish(path, mirror, name, at):
    with sealed_sqlite_snapshot(sqlite_path=path, mirror_root=mirror) as snapshot:
        with closing(sqlite3.connect(snapshot.path)) as conn:
            datasets = [
                d
                for d in CATALOG.values()
                if conn.execute(f"SELECT 1 FROM {d.sqlite_table} LIMIT 1").fetchone()
            ]
        manifests = [
            export_dataset_snapshot(
                dataset=d,
                mirror_root=mirror,
                producer_git_commit="a" * 40,
                source_snapshot=snapshot,
                created_at=at,
            ).manifest_path
            for d in datasets
        ]
    # Require the datasets this fixture actually exported; production's optional
    # article datasets and its nonempty required-inventory validation stay intact.
    policy = ReleasePolicy(
        policy_version=1,
        profile="production",
        datasets=tuple(
            ReleaseDatasetPolicy(
                dataset=dataset.name,
                required=True,
                carries_history=True,
                require_complete_coverage=False,
            )
            for dataset in datasets
        ),
        max_manifest_bytes=16 * 1024 * 1024,
        max_objects=10000,
    )
    manifest, _ = create_release(
        catalog=CATALOG,
        policy=policy,
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id=name,
        created_at=at,
    )
    source = LocalMirrorSource(mirror)
    release = resolve_release(
        source, name, manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest()
    )
    return FixedArticleReader(release, LakeObjectCache(mirror.parent / "cache", source))


def test_200_equal_304_stage_closure_and_conflicting_receipt(tmp_path):
    body = payload()
    first = receipt("first", body)
    second = receipt("equal", body, at=AT + timedelta(hours=1))
    unchanged = ArticleReceipt(
        receipt_id="304",
        source="tsr_data",
        requested_url=URL,
        received_at=AT + timedelta(hours=2),
        status="not_modified",
        http_status=304,
        body_ref=first.body_ref,
    )
    with closing(schema.open_connection(tmp_path / "source.sqlite")) as source, source:
        retain_receipt(source, first, body=body)
        retain_receipt(source, first, body=body)
        retain_receipt(source, second, body=body)
        retain_receipt(source, unchanged)
        _, initial = project_receipt(source, first.receipt_id, generated_at=AT)
        _, later = project_receipt(source, unchanged.receipt_id, generated_at=AT)
        assert initial[0].observation_id != later[0].observation_id
        assert initial[0].article_version_id == later[0].article_version_id
        with pytest.raises(ArticleStoreError, match="different content"):
            retain_receipt(source, first.model_copy(update={"etag": "changed"}), body=body)
        with closing(schema.open_connection(tmp_path / "stage.sqlite")) as stage, stage:
            with pytest.raises(ArticleStoreError, match="not retained"):
                retain_receipt(stage, unchanged)
            copy_receipt_closure(source, stage, "304")
            assert read_receipt(stage, "304") == unchanged
            assert read_body(stage, first.body_ref) == body
            assert stage.execute("SELECT count(*) FROM article_source_receipts").fetchone()[0] == 2
            wrong = unchanged.model_copy(
                update={"receipt_id": "wrong", "requested_url": URL + "?different=1"}
            )
            with pytest.raises(ArticleStoreError, match="representation"):
                retain_receipt(stage, wrong)


def test_failed_parse_empty_and_unknown_publication_are_not_interchangeable(tmp_path):
    with closing(schema.open_connection(tmp_path / "market.sqlite")) as conn, conn:
        for identity, body, expected in (
            ("empty", b"[]", "empty"),
            ("broken", b"not-json", "parse_failed"),
        ):
            retain_receipt(conn, receipt(identity, body), body=body)
            run, observations = project_receipt(conn, identity, generated_at=AT)
            assert run.status == expected
            assert observations == ()
            assert read_body(conn, receipt(identity, body).body_ref) == body
        body = b'[{"url":"/unknown","title":"synthetic"}]'
        retain_receipt(conn, receipt("unknown", body), body=body)
        _, observations = project_receipt(conn, "unknown", generated_at=AT)
        assert observations[0].published.value is None
        assert observations[0].content_scope == "headline"


def test_artifacts_preserve_revision_and_validate_text_coordinate_binding(tmp_path):
    with closing(schema.open_connection(tmp_path / "market.sqlite")) as conn, conn:
        body = payload()
        retain_receipt(conn, receipt("one", body), body=body)
        _, observations = project_receipt(conn, "one", generated_at=AT)
        first = artifact(observations[0])
        second = artifact(observations[0], revision="synthetic-v2", status="failed")
        save_artifact(conn, first)
        save_artifact(conn, second)
        assert read_artifacts(conn, [first.artifact_id, second.artifact_id]) == (first, second)
        with pytest.raises(ArticleStoreError, match="coordinates"):
            save_artifact(conn, first.model_copy(update={"text_sha256": "0" * 64}))
        assert read_observations(conn, [observations[0].observation_id]) == observations


def test_new_release_retains_old_exact_quote_without_claiming_old_release_available(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(schema.open_connection(path)) as conn, conn:
        original = payload()
        retain_receipt(conn, receipt("old", original), body=original)
        _, observations = project_receipt(conn, "old", generated_at=AT)
        first = artifact(observations[0])
        save_artifact(conn, first)
    old = publish(path, tmp_path / "mirror", "old-release", AT)
    listing = old.list(limit=1)
    assert listing["month"] == "2026-10"
    reference = ArticleEvidenceRef.model_validate(listing["items"][0]["reference"]).model_copy(
        update={"artifact_id": first.artifact_id, "artifact_sha256": first.sha256}
    )
    assert old.get(reference).body == original
    assert reference.receipt_month == "2026-10"
    assert reference.body_month == "2026-10"
    with pytest.raises((ArticleStoreError, ValueError)):
        old.get(reference.model_copy(update={"receipt_month": "2026-09"}))
    with closing(schema.open_connection(path)) as conn, conn:
        newer = payload("合成調査は14社へ訂正。")
        retain_receipt(conn, receipt("new", newer, at=AT + timedelta(days=1)), body=newer)
        project_receipt(conn, "new", generated_at=AT + timedelta(days=1))
    current = publish(path, tmp_path / "mirror", "new-release", AT + timedelta(days=1))
    with pytest.raises(ArticleStoreError, match="original fixed release"):
        current.get(reference)
    exact = current.exact_evidence(reference)
    assert exact.body == original
    assert exact.artifact == first
    assert exact.reference.lineage_release_id == "old-release"
    assert exact.lookup_release_id == "new-release"
    with pytest.raises(ArticleStoreError, match="descriptor"):
        current.exact_evidence(reference.model_copy(update={"artifact_sha256": "0" * 64}))
    first_page = current.list(limit=1)
    second_page = current.list(limit=1, after=first_page["next_after"])
    assert first_page["has_more"]
    assert not second_page["has_more"]
    assert first_page["items"] != second_page["items"]


def test_nhk_guid_index_binds_document_and_revalidation_without_guessing(tmp_path):
    feed_url = "https://news.web.nhk/n-data/conf/na/rss/cat4.xml"
    page_url = "https://news.web.nhk/newsweb/na/synthetic"
    feed = (
        f"<rss><channel><item><guid>synthetic-guid</guid><link>{page_url}</link>"
        "<title>Synthetic feed title</title><description>Short summary</description>"
        "</item></channel></rss>"
    ).encode()
    page = b'<div itemprop="articleBody"><p>Synthetic body</p></div>'

    def saved(conn, identity, source, url, body, at):
        item = ArticleReceipt(
            receipt_id=identity,
            source=source,
            requested_url=url,
            received_at=at,
            status="ok",
            http_status=200,
            body_ref=BodyRef(receipt_id=identity, sha256=hashlib.sha256(body).hexdigest()),
        )
        retain_receipt(conn, item, body=body)

    with closing(schema.open_connection(tmp_path / "market.sqlite")) as conn, conn:
        saved(conn, "feed", "nhk_news", feed_url, feed, AT)
        _, (index,) = project_receipt(conn, "feed", generated_at=AT)
        saved(conn, "body", "nhk_news", page_url, page, AT + timedelta(minutes=1))
        _, (first,) = project_receipt(
            conn,
            "body",
            kind="document",
            index_observation_id=index.observation_id,
            generated_at=AT,
        )
        assert first.article_id == index.article_id == "nhk_news:synthetic-guid"
        assert first.title == "Synthetic feed title"
        assert first.native_metadata["index_observation_id"] == index.observation_id
        _, (orphan,) = project_receipt(conn, "body", kind="document", generated_at=AT)
        assert orphan.article_id == f"nhk_news:{page_url}"
        assert "index_observation_id" not in orphan.native_metadata
        saved(conn, "feed-again", "nhk_news", feed_url, feed, AT + timedelta(minutes=2))
        _, (later_index,) = project_receipt(conn, "feed-again", generated_at=AT)
        saved(conn, "again", "nhk_news", page_url, page, AT + timedelta(minutes=3))
        _, (second,) = project_receipt(
            conn,
            "again",
            kind="document",
            index_observation_id=later_index.observation_id,
            generated_at=AT,
        )
        assert second.article_id == first.article_id
        assert second.article_version_id == first.article_version_id
        assert second.observation_id != first.observation_id
        assert second.native_metadata["index_observation_id"] == later_index.observation_id
        saved(
            conn,
            "other-body",
            "nhk_news",
            "https://news.web.nhk/newsweb/na/other",
            page,
            AT + timedelta(minutes=4),
        )
        with pytest.raises(ArticleStoreError, match="does not identify"):
            project_receipt(
                conn,
                "other-body",
                kind="document",
                index_observation_id=index.observation_id,
            )


def test_tsr_document_uses_explicit_feed_title_when_h1_is_missing(tmp_path):
    body_url = "https://www.tsr-net.co.jp/data/detail/synthetic.html"
    feed = payload(title="Synthetic TSR index title")
    body = b'<div class="entry"><p>Synthetic article text</p></div>'
    with closing(schema.open_connection(tmp_path / "market.sqlite")) as conn, conn:
        retain_receipt(conn, receipt("tsr-feed", feed), body=feed)
        _, (index,) = project_receipt(conn, "tsr-feed", generated_at=AT)
        retain_receipt(
            conn,
            ArticleReceipt(
                receipt_id="tsr-body",
                source="tsr_data",
                requested_url=body_url,
                received_at=AT + timedelta(minutes=1),
                status="ok",
                http_status=200,
                body_ref=BodyRef(receipt_id="tsr-body", sha256=hashlib.sha256(body).hexdigest()),
            ),
            body=body,
        )
        _, (document,) = project_receipt(
            conn, "tsr-body", kind="document", index_observation_id=index.observation_id
        )
        assert document.title == "Synthetic TSR index title"
        assert document.article_id == index.article_id


def test_explicit_schema_upgrade_preserves_old_store_and_operator_rows(tmp_path):
    source = tmp_path / "old.sqlite"
    old_ddl = schema._SCHEMA_SQL.replace(ARTICLE_RECEIPTS_DDL, "").replace(
        ARTICLE_OBSERVATIONS_DDL, ""
    )
    with closing(sqlite3.connect(source)) as conn, conn:
        conn.executescript(old_ddl + "PRAGMA user_version=48;")
        conn.execute("CREATE TABLE operator_synthetic(value BLOB)")
        conn.execute("INSERT INTO operator_synthetic VALUES (?)", (b"preserved",))
    before = source.read_bytes()
    with pytest.raises(schema.SQLiteSchemaError, match="expected=49"):
        schema.open_connection(source)
    destination = tmp_path / "upgraded.sqlite"
    upgrade_article_working_copy(source, destination)
    assert source.read_bytes() == before
    with closing(schema.open_connection(destination)) as conn:
        assert conn.execute("SELECT value FROM operator_synthetic").fetchone()[0] == b"preserved"
        assert conn.execute("SELECT count(*) FROM article_source_receipts").fetchone()[0] == 0
    with pytest.raises(FileExistsError):
        upgrade_article_working_copy(source, destination)


def test_cli_validation_does_not_echo_private_input(tmp_path, capsys):
    from baibai_engine.cli.articles import main

    private = tmp_path / "invalid.json"
    private.write_text('{"private_original":"DO_NOT_PRINT_SYNTHETIC_SECRET"}')
    result = main(
        [
            "save-artifact",
            "--artifact-file",
            str(private),
            "--sqlite-path",
            str(tmp_path / "market.sqlite"),
        ]
    )
    captured = capsys.readouterr()
    assert result == 1
    assert "ValidationError" in captured.err
    assert "DO_NOT_PRINT_SYNTHETIC_SECRET" not in captured.err + captured.out


def test_assistant_extraction_requires_fixed_model_prompt_and_config(tmp_path):
    from pydantic import ValidationError

    with closing(schema.open_connection(tmp_path / "market.sqlite")) as conn, conn:
        body = payload()
        retain_receipt(conn, receipt("one", body), body=body)
        _, observations = project_receipt(conn, "one", generated_at=AT)
        draft = artifact(observations[0]).model_dump(mode="json")
        draft["method"] = "assistant"
        with pytest.raises(ValidationError, match="fixed model"):
            ArticleExtractionArtifact.model_validate(draft)
        candidate = ArticleExtractionArtifact.model_validate(
            {
                **draft,
                "model_id": "synthetic-model",
                "prompt_sha256": "a" * 64,
                "config_sha256": "b" * 64,
            }
        )
        save_artifact(conn, candidate)
        assert read_artifacts(conn, [candidate.artifact_id]) == (candidate,)
