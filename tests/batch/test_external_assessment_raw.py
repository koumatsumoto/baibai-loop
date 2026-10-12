"""Temporary diagnostic raw must not become a dependency of typed checkpoints."""

import gzip
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
import requests
from tests.batch.test_external_assessment_daily import FakeObjectStore, checkpoint, plan
from tests.engine.test_external_assessment_sources import monex_html, official_html, rakuten_html

from baibai_batch.jobs import external_assessment_daily as daily
from baibai_batch.storage import external_assessment_raw as external_assessment_raw
from baibai_batch.storage.lake_publish import Boto3R2Store, LakePublishError
from baibai_engine.batch_api import ExternalAssessmentReceivedHTML
from baibai_engine.external_assessment.acquisition import collection
from baibai_engine.external_assessment.acquisition.sources import monex, rakuten
from baibai_engine.external_assessment.store import save_observations
from baibai_engine.storage.observations.schema import open_connection


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
            "external_assessment_collect_pair",
            lambda ticker, **kwargs: collection.collect_pair(
                ticker, **{**kwargs, "session": session}
            ),
        )

    return use


def receipts(store):
    return [
        key
        for key in store.objects
        if key.startswith(external_assessment_raw.RAW_PREFIX) and "/receipts/" in key
    ]


def blobs(store):
    return [
        key
        for key in store.objects
        if key.startswith(external_assessment_raw.RAW_PREFIX) and "/html/sha256/" in key
    ]


def test_exact_bytes_encoding_reparse_and_typed_idempotency(tmp_path, connect):
    body = (monex_html() + "<!-- 日本語の元本文 -->").encode("cp932")
    session = Session([(200, body, "cp932")])
    connect(session)
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    metadata, recovered = external_assessment_raw.read_raw_html(store, receipts(store)[0])
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
        external_assessment_raw.save_raw_html(
            store,
            run_id=p.run_id,
            source="monex",
            ticker="7203",
            request_number=metadata["request_number"],
            received=ExternalAssessmentReceivedHTML(
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
    assert (
        sum(
            key.startswith(external_assessment_raw.RAW_PREFIX) and "/html/sha256/" in key
            for key, *_ in store.puts
        )
        == 1
    )
    decoded = [external_assessment_raw.read_raw_html(store, key) for key in receipts(store)]
    assert all(raw == body for _, raw in decoded)
    assert decoded[0][0]["blob_key"] == decoded[1][0]["blob_key"]
    assert decoded[0][0]["batch_id"] != decoded[1][0]["batch_id"]


def test_rakuten_outer_and_iframe_are_preserved_after_typed_checkpoint(connect):
    outer, inner = official_html().encode(), rakuten_html().encode()
    session = Session([(200, outer, "utf-8"), (200, inner, "utf-8")])
    connect(session)
    p, store = plan(sources=("rakuten",)), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    records = [external_assessment_raw.read_raw_html(store, key) for key in receipts(store)]
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
    typed_checkpoint = max(
        i for i, item in enumerate(store.puts) if item[0].startswith("coordination/")
    )
    for key in receipts(store):
        assert next(i for i, item in enumerate(store.puts) if item[0] == key) > typed_checkpoint


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
    assert external_assessment_raw.read_raw_html(store, receipts(store)[0])[1] == body
    assert checkpoint(store, p).journal.entries[0].state == "failure"
    assert not checkpoint(store, p).journal.observations


def test_normal_null_keeps_its_raw_body_and_existing_typed_classification(connect):
    body = monex_html(value="--").encode()
    session = Session([(200, body, "utf-8")])
    connect(session)
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    assert external_assessment_raw.read_raw_html(store, receipts(store)[0])[1] == body
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
@pytest.mark.parametrize("write", [4, 5])
def test_raw_save_failure_warns_and_preserves_normal_typed_result(connect, capsys, fault, write):
    session = Session(
        [(200, official_html().encode(), "utf-8"), (200, rakuten_html().encode(), "utf-8")]
    )
    connect(session)
    p, store = plan(sources=("rakuten",)), FakeObjectStore()
    store.put_fault = (write, fault)
    daily.acquire_cohort(store, p, 0)
    assert len(session.calls) == 2
    assert checkpoint(store, p).journal.entries[0].state == "success"
    assert len(checkpoint(store, p).journal.observations) == 1
    output = capsys.readouterr()
    assert "::warning::External assessment diagnostic raw save failed" in output.err
    assert "synthetic" not in output.err
    assert "private-cookie-header" not in output.err
    assert "private-authorization-header" not in output.err
    assert output.out == ""


def test_sample_readback_detects_corruption_without_a_per_response_download(connect):
    class BrokenReadback(FakeObjectStore):
        def get_bytes(self, key):
            raw = super().get_bytes(key)
            return (
                gzip.compress(b"wrong raw body", mtime=0)
                if key.startswith(external_assessment_raw.RAW_PREFIX) and "/html/sha256/" in key
                else raw
            )

    session = Session([(200, monex_html().encode(), "utf-8")])
    connect(session)
    p, store = plan(), BrokenReadback()
    daily.acquire_cohort(store, p, 0)
    assert checkpoint(store, p).journal.entries[0].state == "success"
    assert not any(
        action == "GET" and key.startswith(external_assessment_raw.RAW_PREFIX)
        for action, key in store.events
    )
    with pytest.raises(LakePublishError, match="raw body readback mismatch"):
        external_assessment_raw.read_raw_html(store, receipts(store)[0])


def test_normal_capture_and_blob_reuse_do_not_download_raw_objects(connect):
    body = monex_html().encode()
    session = Session([(200, body, "utf-8"), (200, body, "utf-8")])
    connect(session)
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    daily.acquire_cohort(store, replace(p, run_id="another-raw-receipt"), 0)
    assert not any(
        action == "GET" and key.startswith(external_assessment_raw.RAW_PREFIX)
        for action, key in store.events
    )
    assert (
        sum(
            action == "HEAD" and key.startswith(external_assessment_raw.RAW_PREFIX)
            for action, key in store.events
        )
        == 4
    )
    assert len(blobs(store)) == 1
    assert len(receipts(store)) == 2


def test_received_utc_day_controls_blob_sharing_not_batch_date():
    store = FakeObjectStore()
    first = datetime(2026, 10, 10, 23, 59, 59, tzinfo=UTC)
    keys = []
    for number, received in enumerate((first, first + timedelta(seconds=1)), start=1):
        keys.append(
            external_assessment_raw.save_raw_html(
                store,
                run_id="retail-daily-2026-10-09",
                source="monex",
                ticker="7203",
                request_number=number,
                received=ExternalAssessmentReceivedHTML(
                    monex.public_url("7203"), received, "utf-8", b"same"
                ),
            )
        )
    assert len(blobs(store)) == 2
    assert keys[0].startswith(external_assessment_raw.RAW_PREFIX + "utc-date/2026-10-10/receipts/")
    assert keys[1].startswith(external_assessment_raw.RAW_PREFIX + "utc-date/2026-10-11/receipts/")
    old_receipt, _ = external_assessment_raw.read_raw_html(store, keys[0])
    new_receipt, _ = external_assessment_raw.read_raw_html(store, keys[1])
    del store.objects[keys[0]], store.objects[old_receipt["blob_key"]]
    assert external_assessment_raw.read_raw_html(store, keys[1]) == (new_receipt, b"same")
    jst = (first + timedelta(seconds=1)).astimezone(timezone(timedelta(hours=9)))
    same_day = external_assessment_raw.save_raw_html(
        store,
        run_id="new-batch",
        source="monex",
        ticker="7203",
        request_number=1,
        received=ExternalAssessmentReceivedHTML(monex.public_url("7203"), jst, "utf-8", b"same"),
    )
    assert (
        external_assessment_raw.read_raw_html(store, same_day)[0]["blob_key"]
        == new_receipt["blob_key"]
    )
    assert len(blobs(store)) == 1


def test_raw_latency_and_failure_are_outside_typed_pair_deadline(connect, monkeypatch, capsys):
    session = Session([(200, monex_html().encode(), "utf-8")])
    connect(session)
    p, store = plan(), FakeObjectStore()
    elapsed = []

    def slow_failed_save(*args, **kwargs):
        assert checkpoint(store, p).journal.entries[0].state == "success"
        start = collection.time.monotonic()
        collection.time.sleep(collection.DEFAULT_PAIR_TIME_LIMIT_SECONDS + 1)
        elapsed.append(collection.time.monotonic() - start)
        raise LakePublishError("private exception and signed URL must not be logged")

    monkeypatch.setattr(daily, "save_raw_html", slow_failed_save)
    report = daily.acquire_cohort(store, p, 0)
    assert report["stop_reason"] is None
    assert checkpoint(store, p).journal.entries[0].state == "success"
    assert len(checkpoint(store, p).journal.observations) == 1
    assert elapsed == [collection.DEFAULT_PAIR_TIME_LIMIT_SECONDS + 1]
    warning = capsys.readouterr().err
    assert "::warning::" in warning
    assert "signed URL" not in warning
    assert "private exception" not in warning


@pytest.mark.parametrize("write", [1, 2, 3])
def test_typed_checkpoint_failure_still_stops_without_optional_raw_save(connect, write):
    session = Session(
        [(200, official_html().encode(), "utf-8"), (200, rakuten_html().encode(), "utf-8")]
    )
    connect(session)
    p, store = plan(sources=("rakuten",)), FakeObjectStore()
    store.put_fault = (write, "before")
    with pytest.raises(RuntimeError, match="pair_checkpoint_callback_failed"):
        daily.acquire_cohort(store, p, 0)
    assert not receipts(store)
    assert not blobs(store)


def test_inventory_three_utc_dates_unclassified_and_exact_date_prefixes():
    inventory = external_assessment_raw.retention_inventory(
        [
            (
                external_assessment_raw.RAW_PREFIX + "utc-date/2026-10-09/html/sha256/hash.html.gz",
                100,
            ),
            (
                external_assessment_raw.RAW_PREFIX
                + "utc-date/2026-10-09/receipts/batch/monex/7203/1.json",
                10,
            ),
            (
                external_assessment_raw.RAW_PREFIX + "utc-date/2026-10-10/html/sha256/hash.html.gz",
                200,
            ),
            (
                external_assessment_raw.RAW_PREFIX + "utc-date/2026-10-11/html/sha256/hash.html.gz",
                300,
            ),
            (
                external_assessment_raw.RAW_PREFIX + "utc-date/2026-10-12/html/sha256/hash.html.gz",
                400,
            ),
            (external_assessment_raw.RAW_PREFIX + "unclassified.html.gz", 500),
            (external_assessment_raw.RAW_PREFIX + "unclassified.json", 50),
        ],
        as_of=date(2026, 10, 12),
    )
    assert inventory["keep_from_utc_date"] == "2026-10-10"
    assert inventory["unclassified"] == {"objects": 2, "bytes": 550}
    days = inventory["dates"]
    assert days["2026-10-09"]["status"] == "delete_candidate"
    assert days["2026-10-09"]["objects"] == 2
    assert days["2026-10-09"]["bytes"] == 110
    assert (
        days["2026-10-09"]["prefix"] == external_assessment_raw.RAW_PREFIX + "utc-date/2026-10-09/"
    )
    assert all(days[day]["status"] == "keep" for day in ("2026-10-10", "2026-10-11", "2026-10-12"))


def test_inventory_cli_lists_only_raw_root_without_mutations(monkeypatch, capsys):
    class InventoryStore:
        def __init__(self, *, bucket):
            assert bucket == "baibai-stores"
            self.prefixes = []

        def iter_objects(self, prefix):
            self.prefixes.append(prefix)
            return iter(())

    store = InventoryStore(bucket="baibai-stores")
    monkeypatch.delenv("R2_STORES_BUCKET", raising=False)
    monkeypatch.setattr(external_assessment_raw, "Boto3R2Store", lambda **kwargs: store)
    assert external_assessment_raw.main(["--as-of", "2026-10-12"]) == 0
    assert store.prefixes == [external_assessment_raw.RAW_PREFIX]
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "read_only_inventory"
    assert report["dates"] == {}


def test_inventory_failure_does_not_log_sensitive_exception(monkeypatch, capsys):
    def failed_store(**kwargs):
        raise ValueError("private credential and signed URL")

    monkeypatch.setattr(external_assessment_raw, "Boto3R2Store", failed_store)
    assert external_assessment_raw.main([]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "External Assessment raw inventory failed; type=ValueError\n"


def test_r2_inventory_paginates_keys_and_sizes_without_getting_bodies(monkeypatch):
    calls = []

    class Paginator:
        def paginate(self, **kwargs):
            calls.append(kwargs)
            yield {"Contents": [{"Key": "first", "Size": 10}]}
            yield {}
            yield {"Contents": [{"Key": "second", "Size": 20}]}

    class Client:
        def get_paginator(self, name):
            assert name == "list_objects_v2"
            return Paginator()

    store = Boto3R2Store(
        bucket="synthetic",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "synthetic-key",
            "R2_SECRET_ACCESS_KEY": "synthetic-secret",
        },
    )
    monkeypatch.setattr(store, "_client", lambda: Client())
    assert list(store.iter_objects(external_assessment_raw.RAW_PREFIX)) == [
        ("first", 10),
        ("second", 20),
    ]
    assert calls == [{"Bucket": "synthetic", "Prefix": external_assessment_raw.RAW_PREFIX}]
