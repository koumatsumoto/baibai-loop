"""Canonical raw bundles are durable prerequisites of typed cohort checkpoints."""

import io
import json
import tarfile
import uuid
from datetime import date, datetime

import pytest
import requests
from tests.engine.test_external_assessment_sources import monex_html, official_html, rakuten_html
from tests.jobs.external_assessment_response_fixtures import response
from tests.jobs.test_external_assessment_daily import FakeObjectStore, plan, stage

import baibai_jobs.acquisition.external_assessments.collection as collection
from baibai_engine.machine_analysis.observations.external_assessments.normalization import (
    ParseError,
    monex,
    rakuten,
)
from baibai_engine.machine_analysis.observations.external_assessments.normalization.decode import decode_attempt
from baibai_engine.machine_analysis.observations.external_assessments.store import save_observations
from baibai_jobs.jobs import external_assessment_daily as daily
from baibai_jobs.storage import external_assessment_raw as external_assessment_raw
from baibai_jobs.storage import external_assessment_staging as staging
from baibai_jobs.storage.lake_publish import Boto3R2Store, LakePublishError


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


def bundles(store):
    return [
        key
        for key in store.objects
        if key.startswith(external_assessment_raw.RAW_PREFIX) and "/bundles/" in key
    ]


def stage_or_none(store, plan):
    return staging.read_stage(store, plan, 0)


def test_exact_bytes_encoding_reparse_and_typed_idempotency(tmp_path, connect):
    body = (monex_html() + "<!-- 日本語の元本文 -->").encode("cp932")
    session = Session([(200, body, "cp932")])
    connect(session)
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    saved = stage(store, p)
    metadata, recovered = external_assessment_raw.read_raw_bundle(store, saved.raw_bundle_keys[0])
    assert tuple(item.body for item in recovered) == (body,)
    receipt = metadata["receipts"][0]
    assert receipt["selected_encoding"] == "cp932"
    assert receipt["requested_url"] == monex.public_url("7203")
    assert saved.result.entries[0].attempt_id == recovered[0].attempt_id
    observation = monex.parse_html(
        recovered[0].body.decode(receipt["selected_encoding"]),
        ticker="7203",
        source_url=receipt["requested_url"],
        fetched_at_utc=datetime.fromisoformat(receipt["received_at_utc"]),
    )
    assert saved.result.observations == (observation,)
    path = tmp_path / "observations.sqlite3"
    assert save_observations(path, [observation]) == 1
    assert save_observations(path, [observation]) == 0
    manifest_bytes = json.dumps(metadata).encode()
    assert b"private-cookie-header" not in manifest_bytes
    assert b"private-authorization-header" not in manifest_bytes
    assert len(store.puts) == 2
    assert store.puts[0][0] == saved.raw_bundle_keys[0]
    assert store.puts[1][0].endswith("cohort-0000.json")


def test_rakuten_outer_and_iframe_bytes_are_in_one_archive(connect):
    outer, iframe = official_html().encode(), rakuten_html().encode()
    session = Session([(200, outer, "utf-8"), (200, iframe, "utf-8")])
    connect(session)
    p, store = plan(sources=("rakuten",)), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    saved = stage(store, p)
    manifest, bodies = external_assessment_raw.read_raw_bundle(store, saved.raw_bundle_keys[0])
    assert tuple(item.body for item in bodies) == (outer, iframe)
    receipt = manifest["receipts"][1]
    assert bodies[0].attempt_id == bodies[1].attempt_id == saved.result.entries[0].attempt_id
    assert bodies[1].parent_receipt_id == bodies[0].receipt_id
    parsed = rakuten.parse_html(
        bodies[1].body.decode(),
        ticker="7203",
        source_url=receipt["requested_url"],
        fetched_at_utc=datetime.fromisoformat(receipt["received_at_utc"]),
    )
    assert saved.result.observations == (parsed,)
    assert len(store.puts) == 2


@pytest.mark.parametrize("status", [403, 404, 500])
def test_non200_has_no_raw_bundle(connect, status):
    connect(Session([(status, b"denied", "utf-8")]))
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    assert stage(store, p).result.entries[0].state == "failure"
    assert stage(store, p).result.entries[0].attempt_id is not None
    assert stage(store, p).raw_bundle_keys == ()
    assert bundles(store) == []


@pytest.mark.parametrize(
    ("html", "state"), [("invalid public HTML", "failure"), (monex_html(value="--"), "normal_null")]
)
def test_parse_failure_and_normal_null_preserve_original_bytes(connect, html, state):
    connect(Session([(200, html.encode(), "utf-8")]))
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    saved = stage(store, p)
    assert saved.result.entries[0].state == state
    assert tuple(
        item.body
        for item in external_assessment_raw.read_raw_bundle(store, saved.raw_bundle_keys[0])[1]
    ) == (html.encode(),)


def test_decode_error_keeps_exact_received_bytes_before_failed_parse(connect):
    connect(Session([(200, b"\xff source bytes", "utf-8")]))
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    saved = stage(store, p)
    entry = saved.result.entries[0]
    assert (entry.state, entry.reason) == ("failure", "response_encoding")
    _, responses = external_assessment_raw.read_raw_bundle(store, saved.raw_bundle_keys[0])
    assert len(responses) == 1
    assert responses[0].body == b"\xff source bytes"
    assert responses[0].attempt_id == entry.attempt_id


@pytest.mark.parametrize("fault", ["before", "after", "bad_receipt"])
def test_raw_upload_failure_prevents_stage_commit(connect, capsys, fault):
    connect(Session([(200, monex_html().encode(), "utf-8")]))
    p, store = plan(), FakeObjectStore()
    store.put_fault = (1, fault)
    with pytest.raises(LakePublishError):
        daily.acquire_cohort(store, p, 0)
    assert stage_or_none(store, p) is None
    assert len(store.puts) == 1
    assert "private secret" not in capsys.readouterr().err


@pytest.mark.parametrize("method", ["append", "finish"])
def test_local_raw_failure_prevents_stage_commit(connect, monkeypatch, capsys, method):
    connect(Session([(200, monex_html().encode(), "utf-8")]))

    def fail(*a, **k):
        raise OSError("private secret must not be logged")

    monkeypatch.setattr(external_assessment_raw.RawBundle, method, fail)
    p, store = plan(), FakeObjectStore()
    with pytest.raises(OSError, match="private secret must not be logged"):
        daily.acquire_cohort(store, p, 0)
    assert stage_or_none(store, p) is None
    assert store.puts == []
    error = capsys.readouterr().err
    assert "private secret" not in error


def test_raw_upload_precedes_typed_checkpoint_and_never_downloads_per_body(connect, monkeypatch):
    connect(Session([(200, monex_html().encode(), "utf-8")]))
    p, store = plan(), FakeObjectStore()
    original = store.put_file

    def put(key, path, **kwargs):
        if key.startswith(external_assessment_raw.RAW_PREFIX):
            assert stage_or_none(store, p) is None
        else:
            assert bundles(store)
        return original(key, path, **kwargs)

    monkeypatch.setattr(store, "put_file", put)
    daily.acquire_cohort(store, p, 0)
    assert not any(
        method == "GET" and key.startswith(external_assessment_raw.RAW_PREFIX)
        for method, key in store.events
    )


def test_three_media_normal_and_rakuten_retry_fixture_bundle_sizing_and_restoration():
    from tests.engine.test_kabumap import html as kabumap_html

    pages = [
        monex_html().encode(),
        official_html().encode(),
        rakuten_html().encode(),
        kabumap_html().encode(),
    ]
    store = FakeObjectStore()
    # 50 tickers * (1 Monex + 2 Rakuten + 1 Kabumap + 5 retried Rakuten outer pages).
    expected = []
    with external_assessment_raw.RawBundle("synthetic-sizing", 0) as bundle:
        for i in range(50):
            ticker = str(1000 + i)
            monex_page = response("monex", ticker, pages[0])
            rakuten_attempt = str(uuid.uuid4())
            rakuten_page = response("rakuten", ticker, pages[1], attempt_id=rakuten_attempt)
            rakuten_iframe = response(
                "rakuten",
                ticker,
                pages[2],
                attempt_id=rakuten_attempt,
                role="iframe",
                parent_receipt_id=rakuten_page.receipt_id,
            )
            kabumap_page = response("kabumap", ticker, pages[3])
            for item in (monex_page, rakuten_page, rakuten_iframe, kabumap_page):
                expected.append(item.body)
                bundle.append(item)
            for _ in range(5):
                retry_page = response("rakuten", ticker, pages[1])
                expected.append(retry_page.body)
                bundle.append(retry_page)
        key = bundle.finish()
        bundle.upload(store)
        archive_bytes = bundle.path.stat().st_size
        raw_bytes = sum(map(len, expected))
        assert archive_bytes < raw_bytes
        assert len(store.puts) == 1
        manifest, restored = external_assessment_raw.read_raw_bundle(store, key)
        assert len(manifest["receipts"]) == 450
        assert tuple(item.body for item in restored) == tuple(expected)


def test_inventory_three_utc_dates_unclassified_and_exact_date_prefixes():
    inventory = external_assessment_raw.retention_inventory(
        [
            (
                external_assessment_raw.RAW_PREFIX + "utc-date/2026-10-09/html/sha256/hash.html.gz",
                100,
            ),
            (
                external_assessment_raw.RAW_PREFIX
                + "utc-date/2026-10-09/receipts/jobs/monex/7203/1.json",
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


def test_authorized_response_is_never_archived_or_treated_as_typed_success(connect):
    class Authenticated(Session):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            response.request.headers["Authorization"] = "private synthetic credential"
            return response

    session = Authenticated([(200, monex_html().encode(), "utf-8")])
    connect(session)
    store = FakeObjectStore()
    with pytest.raises(RuntimeError, match="requires_anonymous_response"):
        daily.acquire_cohort(store, plan(), 0)
    assert store.puts == []
    assert len(session.calls) == 1


def test_bundle_readback_rejects_changed_body_hash_without_extracting_paths(connect):
    import io
    import tarfile

    connect(Session([(200, monex_html().encode(), "utf-8")]))
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    key = stage(store, p).raw_bundle_keys[0]
    with tarfile.open(fileobj=io.BytesIO(store.objects[key][0]), mode="r:gz") as original:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as changed:
            for member in original.getmembers():
                stream = original.extractfile(member)
                body = stream.read()
                if member.name.startswith("bodies/"):
                    body = b"synthetic corruption"
                    member.size = len(body)
                changed.addfile(member, io.BytesIO(body))
    store.seed(key, output.getvalue(), "application/gzip")
    with pytest.raises(LakePublishError, match="body readback mismatch"):
        external_assessment_raw.read_raw_bundle(store, key)


def _rewrite_manifest(store, key, change):
    with tarfile.open(fileobj=io.BytesIO(store.objects[key][0]), mode="r:gz") as original:
        members = [(item.name, original.extractfile(item).read()) for item in original.getmembers()]
    manifest = json.loads(members[0][1])
    change(manifest)
    members[0] = ("manifest.json", json.dumps(manifest).encode())
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, body in members:
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    store.seed(key, output.getvalue(), "application/gzip")


@pytest.mark.parametrize(
    "change",
    [
        lambda manifest: manifest.update(format="external-assessment-cohort-raw-v1"),
        lambda manifest: manifest["receipts"][1].update(parent_receipt_id=str(uuid.uuid4())),
        lambda manifest: manifest["receipts"][1].update(attempt_id=str(uuid.uuid4())),
        lambda manifest: manifest["receipts"][0].update(extra="unexpected"),
        lambda manifest: manifest["receipts"][0].update(requested_ticker="７２０３"),
    ],
)
def test_readback_rejects_old_or_mismatched_raw_identity(connect, change):
    connect(
        Session([(200, official_html().encode(), "utf-8"), (200, rakuten_html().encode(), "utf-8")])
    )
    p, store = plan(sources=("rakuten",)), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    key = stage(store, p).raw_bundle_keys[0]
    _rewrite_manifest(store, key, change)
    with pytest.raises(ValueError, match=r"external assessment|canonical"):
        external_assessment_raw.read_raw_bundle(store, key)


def test_outer_page_only_is_valid_original_but_incomplete_for_l2():
    store = FakeObjectStore()
    outer = response("rakuten", "7203", official_html().encode())
    with external_assessment_raw.RawBundle("outer-only", 0) as bundle:
        bundle.append(outer)
        key = bundle.finish()
        bundle.upload(store)
    _, restored = external_assessment_raw.read_raw_bundle(store, key)
    assert restored == (outer,)
    with pytest.raises(ParseError, match="incomplete_attempt"):
        decode_attempt(restored)
