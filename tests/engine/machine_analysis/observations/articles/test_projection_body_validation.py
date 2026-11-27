"""一覧の全項目は同じ原本へ結び付け、保存時の本文検証だけ共有する。"""

import gzip
import json
from contextlib import closing
from datetime import timedelta
from hashlib import sha256

import pytest
from tests.engine.machine_analysis.observations.articles.test_article_storage import (
    AT,
    payload,
    receipt,
)

from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt, BodyRef
from baibai_engine.data_collection.receipts.articles.store import ArticleStoreError, retain_receipt
from baibai_engine.machine_analysis.observations.articles import store
from baibai_engine.machine_analysis.observations.articles.projection import project_receipt
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


@pytest.mark.parametrize(
    ("kind", "path", "html", "expected_text"),
    [
        ("index", "/news", '<a href="/news/123">合成記事</a>', ""),
        (
            "document",
            "/news/123",
            '<h1>合成記事</h1><div class="news-body">記事の本文</div>',
            "記事の本文",
        ),
    ],
)
@pytest.mark.parametrize(
    ("body_encoding", "original_encoding", "repeat_encoding"),
    [
        ("cp932", "cp932", None),
        ("cp932", "cp932", "utf-8"),
        ("utf-8", None, "cp932"),
        ("utf-8", None, None),
    ],
)
def test_not_modified_reuses_body_owner_encoding(
    tmp_path, kind, path, html, expected_text, body_encoding, original_encoding, repeat_encoding
):
    body = html.encode(body_encoding)
    original = ArticleReceipt(
        receipt_id="original",
        source="minkabu_news",
        requested_url=f"https://minkabu.jp{path}",
        received_at=AT,
        status="ok",
        http_status=200,
        encoding=original_encoding,
        body_ref=BodyRef(receipt_id="original", sha256=sha256(body).hexdigest()),
    )
    repeated = ArticleReceipt(
        receipt_id="unchanged",
        source=original.source,
        requested_url=original.requested_url,
        received_at=AT + timedelta(hours=1),
        status="not_modified",
        http_status=304,
        encoding=repeat_encoding,
        body_ref=original.body_ref,
    )
    with closing(open_connection(tmp_path / "market.sqlite")) as conn, conn:
        retain_receipt(conn, original, body=body)
        retain_receipt(conn, repeated)
        first_run, (first,) = project_receipt(conn, original.receipt_id, kind=kind)
        run, observations = project_receipt(conn, repeated.receipt_id, kind=kind)
        assert first_run.status == run.status == "parsed"
        (observation,) = observations
        assert observation.receipt_id == repeated.receipt_id
        assert observation.receipt_sha256 == repeated.sha256
        assert observation.body_ref == first.body_ref == original.body_ref
        assert observation.observation_id != first.observation_id
        assert observation.article_version_id == first.article_version_id
        assert observation.title == first.title == "合成記事"
        assert observation.text == first.text == expected_text


@pytest.mark.parametrize("count", [0, 1, 3])
@pytest.mark.parametrize("not_modified", [False, True])
def test_projection_validates_shared_body_once_and_keeps_replay_identity(
    tmp_path, monkeypatch, count, not_modified
):
    body = json.dumps(
        [
            {**json.loads(payload())[0], "url": f"/data/detail/synthetic-{index}.html"}
            for index in range(count)
        ]
    ).encode()
    with closing(open_connection(tmp_path / "market.sqlite")) as conn, conn:
        original = receipt("original", body)
        retain_receipt(conn, original, body=body)
        selected = original
        if not_modified:
            selected = ArticleReceipt(
                receipt_id="unchanged",
                source=original.source,
                requested_url=original.requested_url,
                received_at=AT + timedelta(hours=1),
                status="not_modified",
                http_status=304,
                body_ref=original.body_ref,
            )
            retain_receipt(conn, selected)
        checked = []
        read_body = store.read_body

        def check_body(connection, ref):
            checked.append(ref)
            return read_body(connection, ref)

        monkeypatch.setattr(store, "read_body", check_body)
        run, observations = project_receipt(
            conn, selected.receipt_id, generated_at=AT + timedelta(hours=2)
        )
        assert checked == ([original.body_ref] if count else [])
        assert len(observations) == count
        assert run.status == ("parsed" if count else "empty")
        assert all(item.receipt_id == selected.receipt_id for item in observations)
        assert all(item.body_ref == original.body_ref for item in observations)
        before = list(conn.iterdump())
        checked.clear()
        replay = project_receipt(conn, selected.receipt_id, generated_at=AT + timedelta(hours=3))
        assert replay == (run, observations)
        assert list(conn.iterdump()) == before
        assert checked == ([original.body_ref] if count else [])


@pytest.mark.parametrize("damage", ["missing_body", "changed_body", "second_observation"])
def test_shared_validation_still_rejects_missing_body_and_every_binding(tmp_path, damage):
    body = json.dumps(json.loads(payload()) * 2).encode()
    with closing(open_connection(tmp_path / "market.sqlite")) as conn, conn:
        original = receipt("original", body)
        retain_receipt(conn, original, body=body)
        run, observations = project_receipt(conn, original.receipt_id, generated_at=AT)
        conn.execute("DELETE FROM article_observations")
        conn.execute("DELETE FROM article_projection_runs")
        if damage == "second_observation":
            observations = (
                observations[0],
                observations[1].model_copy(update={"source": "another_source"}),
            )
            run = run.model_copy(
                update={"observation_ids": tuple(o.observation_id for o in observations)}
            )
        else:
            conn.execute(
                "UPDATE article_source_receipts SET body_gzip=? WHERE receipt_id=?",
                (
                    None if damage == "missing_body" else gzip.compress(b"changed"),
                    original.receipt_id,
                ),
            )
        before = list(conn.iterdump())
        with pytest.raises(ArticleStoreError, match=r"missing|digest mismatch|differs from source"):
            store.save_projection(conn, run, observations)
        assert list(conn.iterdump()) == before
