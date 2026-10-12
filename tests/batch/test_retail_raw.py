"""Raw preservation through the real collector and existing private checkpoint path."""

import gzip
import json
from dataclasses import replace
from datetime import datetime

import pytest
import requests
from tests.batch.test_retail_daily import FakeObjectStore, checkpoint, plan
from tests.engine.test_retail_dynamics_sources import monex_html, official_html, rakuten_html

from baibai_batch.jobs import retail_daily as daily
from baibai_batch.storage import retail_raw
from baibai_batch.storage.lake_publish import LakePublishError
from baibai_engine.batch_api import RetailReceivedHTML
from baibai_engine.market.retail_dynamics import collection
from baibai_engine.market.retail_dynamics.sources import monex, rakuten
from baibai_engine.market.retail_dynamics.store import save_observations
from baibai_engine.market.sqlite.schema import open_connection


class Session(requests.Session):
    def __init__(self, replies):
        super().__init__()
        self.replies = iter(replies)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        assert kwargs["allow_redirects"] is False
        status, body, encoding = next(self.replies)
        result = requests.Response()
        result.status_code = status
        result._content = body
        result._content_consumed = True
        result.request = requests.Request("GET", url).prepare()
        result.headers.update(
            {
                "Content-Type": f"text/html; charset={encoding}",
                "Set-Cookie": "private-cookie-header",
                "Authorization": "private-authorization-header",
            }
        )
        return result


@pytest.fixture
def connect(monkeypatch):
    timer = [0.0]
    monkeypatch.setattr(collection.time, "monotonic", lambda: timer[0])
    monkeypatch.setattr(
        collection.time, "sleep", lambda seconds: timer.__setitem__(0, timer[0] + seconds)
    )

    def use(session):
        monkeypatch.setattr(
            daily,
            "retail_collect_pair",
            lambda ticker, **kwargs: collection.collect_pair(ticker, session=session, **kwargs),
        )

    return use


def receipts(store):
    return [key for key in store.objects if key.startswith("retail-dynamics/raw-receipts/")]


def blobs(store):
    return [key for key in store.objects if key.startswith(retail_raw.BLOB_PREFIX)]


def test_exact_bytes_encoding_reparse_and_typed_idempotency(tmp_path, connect):
    body = (monex_html() + "<!-- 日本語の元本文 -->").encode("cp932")
    session = Session([(200, body, "cp932")])
    connect(session)
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    metadata, recovered = retail_raw.read_raw_html(store, receipts(store)[0])
    assert recovered == body
    assert metadata["encoding"] == "cp932"
    assert metadata["url"] == monex.public_url("7203")
    assert metadata["distributor"] == "monex"
    assert metadata["requested_ticker"] == "7203"
    assert metadata["request_number"] == 1
    assert metadata["body_bytes"] == len(body)
    saved = checkpoint(store, p)
    reparsed = monex.parse_html(
        recovered.decode(metadata["encoding"]),
        ticker=metadata["requested_ticker"],
        source_url=metadata["url"],
        fetched_at_utc=datetime.fromisoformat(metadata["fetched_at_utc"]),
    )
    assert saved.journal.observations == (reparsed,)
    operations = len(store.puts)
    assert (
        retail_raw.save_raw_html(
            store,
            run_id=p.run_id,
            source="monex",
            ticker="7203",
            request_number=metadata["request_number"],
            received=RetailReceivedHTML(
                metadata["url"],
                datetime.fromisoformat(metadata["fetched_at_utc"]),
                metadata["encoding"],
                body,
            ),
        )
        == receipts(store)[0]
    )
    assert len(store.puts) == operations
    assert "private-cookie-header" not in json.dumps(metadata)
    assert "private-authorization-header" not in json.dumps(metadata)
    assert all(
        marker not in recovered
        for marker in (b"private-cookie-header", b"private-authorization-header")
    )
    db = tmp_path / "market.sqlite"
    open_connection(db).close()
    assert save_observations(db, saved.journal.observations) == 1
    assert save_observations(db, saved.journal.observations) == 0
    operations = len(store.puts)
    daily.acquire_cohort(store, p, 0)
    assert len(store.puts) == operations
    assert len(session.calls) == 1
    assert checkpoint(store, p).journal.observations == (reparsed,)


def test_identical_html_across_received_batches_reuses_one_blob(connect):
    body = monex_html().encode()
    session = Session([(200, body, "utf-8"), (200, body, "utf-8")])
    connect(session)
    store, first = FakeObjectStore(), plan()
    daily.acquire_cohort(store, first, 0)
    second = replace(first, run_id="second-raw-batch")
    daily.acquire_cohort(store, second, 0)
    assert len(blobs(store)) == 1
    assert len(receipts(store)) == 2
    assert sum(key.startswith(retail_raw.BLOB_PREFIX) for key, *_ in store.puts) == 1
    decoded = [retail_raw.read_raw_html(store, key) for key in receipts(store)]
    assert all(raw == body for _, raw in decoded)
    assert decoded[0][0]["blob_key"] == decoded[1][0]["blob_key"]
    assert decoded[0][0]["batch_id"] != decoded[1][0]["batch_id"]


def test_rakuten_outer_and_iframe_are_preserved_before_typed_checkpoint(connect):
    outer, inner = official_html().encode(), rakuten_html().encode()
    session = Session([(200, outer, "utf-8"), (200, inner, "utf-8")])
    connect(session)
    p, store = plan(sources=("rakuten",)), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    records = [retail_raw.read_raw_html(store, key) for key in receipts(store)]
    assert [raw for _, raw in records] == [outer, inner]
    assert [item["request_number"] for item, _ in records] == [1, 2]
    assert [item["url"] for item, _ in records] == session.calls
    metadata, html = records[-1]
    parsed = rakuten.parse_html(
        html.decode(metadata["encoding"]),
        ticker="7203",
        source_url=metadata["url"],
        fetched_at_utc=datetime.fromisoformat(metadata["fetched_at_utc"]),
    )
    assert checkpoint(store, p).journal.observations == (parsed,)
    final = store.puts[-1]
    assert final[0].startswith("coordination/retail-dynamics/")
    for key in receipts(store):
        assert next(i for i, item in enumerate(store.puts) if item[0] == key) < len(store.puts) - 1


@pytest.mark.parametrize("status", [302, 403, 429, 500])
def test_non200_has_no_raw_success_receipt(connect, status, monkeypatch):
    session = Session([(status, b"failure body", "utf-8")])
    connect(session)
    monkeypatch.setattr(daily, "_wait_for_retry", lambda *args, **kwargs: False)
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    assert not receipts(store)
    assert not blobs(store)
    entry = checkpoint(store, p).journal.entries[0]
    assert entry.state == "failure"
    assert entry.http_status == status
    assert not checkpoint(store, p).journal.observations


def test_parse_failure_raw_receipt_is_not_a_normal_observation(connect):
    body = b"<html>anonymous unconfirmed page</html>"
    session = Session([(200, body, "utf-8")])
    connect(session)
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    assert retail_raw.read_raw_html(store, receipts(store)[0])[1] == body
    assert checkpoint(store, p).journal.entries[0].state == "failure"
    assert not checkpoint(store, p).journal.observations


def test_normal_null_keeps_its_raw_body_and_existing_typed_classification(connect):
    body = monex_html(value="--").encode()
    session = Session([(200, body, "utf-8")])
    connect(session)
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    assert retail_raw.read_raw_html(store, receipts(store)[0])[1] == body
    saved = checkpoint(store, p)
    assert saved.journal.entries[0].state == "normal_null"
    assert len(saved.journal.observations) == 1


def test_private_iframe_route_is_not_requested_or_archived(connect):
    body = (
        official_html()
        .replace("https://rakuten-sec.ifis.co.jp/index.php", "https://private.example/account")
        .encode()
    )
    session = Session([(200, body, "utf-8")])
    connect(session)
    p, store = plan(sources=("rakuten",)), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    assert session.calls == [rakuten.public_url("7203")]
    assert len(receipts(store)) == 1
    assert checkpoint(store, p).journal.entries[0].state == "failure"


def test_authenticated_response_is_not_archived(connect):
    class Authenticated(Session):
        def get(self, url, **kwargs):
            result = super().get(url, **kwargs)
            result.request = requests.Request(
                "GET", url, headers={"Authorization": "private-authentication"}
            ).prepare()
            return result

    session = Authenticated([(200, monex_html().encode(), "utf-8")])
    connect(session)
    p, store = plan(), FakeObjectStore()
    with pytest.raises(RuntimeError, match="raw_capture_requires_anonymous_response"):
        daily.acquire_cohort(store, p, 0)
    assert not receipts(store)
    assert not blobs(store)
    assert checkpoint(store, p).journal.entries[0].state == "uncertain"


@pytest.mark.parametrize("fault", ["before", "after", "cas", "bad_receipt"])
@pytest.mark.parametrize("write", [2, 3])
def test_raw_save_failure_leaves_inflight_and_stops_before_second_http(connect, fault, write):
    session = Session([(200, official_html().encode(), "utf-8")])
    connect(session)
    p, store = plan(sources=("rakuten",)), FakeObjectStore()
    store.put_fault = (write, fault)
    with pytest.raises(RuntimeError, match="pair_checkpoint_callback_failed"):
        daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == 1
    assert checkpoint(store, p).journal.entries[0].state == "uncertain"
    assert not checkpoint(store, p).journal.observations


def test_sample_readback_detects_corruption_without_a_per_response_download(connect):
    class BrokenReadback(FakeObjectStore):
        def get_bytes(self, key):
            raw = super().get_bytes(key)
            return (
                gzip.compress(b"wrong raw body", mtime=0)
                if key.startswith(retail_raw.BLOB_PREFIX)
                else raw
            )

    session = Session([(200, monex_html().encode(), "utf-8")])
    connect(session)
    p, store = plan(), BrokenReadback()
    daily.acquire_cohort(store, p, 0)
    assert checkpoint(store, p).journal.entries[0].state == "success"
    assert not any(
        action == "GET" and key.startswith("retail-dynamics/") for action, key in store.events
    )
    with pytest.raises(LakePublishError, match="raw body readback mismatch"):
        retail_raw.read_raw_html(store, receipts(store)[0])


def test_normal_capture_and_blob_reuse_do_not_download_raw_objects(connect):
    body = monex_html().encode()
    session = Session([(200, body, "utf-8"), (200, body, "utf-8")])
    connect(session)
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    daily.acquire_cohort(store, replace(p, run_id="another-raw-receipt"), 0)
    assert not any(
        action == "GET" and key.startswith("retail-dynamics/") for action, key in store.events
    )
    assert (
        sum(action == "HEAD" and key.startswith("retail-dynamics/") for action, key in store.events)
        == 4
    )
    assert len(blobs(store)) == 1
    assert len(receipts(store)) == 2
