"""一覧の全項目は同じ原本へ結び付け、保存時の本文検証だけ共有する。"""

import gzip
import json
from contextlib import closing
from datetime import timedelta

import pytest
from tests.engine.machine_analysis.observations.articles.test_article_storage import (
    AT,
    payload,
    receipt,
)

from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt
from baibai_engine.data_collection.receipts.articles.store import ArticleStoreError, retain_receipt
from baibai_engine.machine_analysis.observations.articles import store
from baibai_engine.machine_analysis.observations.articles.projection import project_receipt
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


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
