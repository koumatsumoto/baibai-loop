"""Temporary diagnostic raw must not become a dependency of typed checkpoints."""

import json
from datetime import UTC, date, datetime

import pytest
import requests
from tests.batch.test_external_assessment_daily import FakeObjectStore, plan, stage
from tests.engine.test_external_assessment_sources import monex_html, official_html, rakuten_html

from baibai_batch.jobs import external_assessment_daily as daily
from baibai_batch.storage import external_assessment_raw as external_assessment_raw
from baibai_batch.storage.lake_publish import Boto3R2Store, LakePublishError
from baibai_engine.batch_api import ExternalAssessmentReceivedHTML
from baibai_engine.external_assessment.acquisition import collection
from baibai_engine.external_assessment.acquisition.sources import monex, rakuten
from baibai_engine.external_assessment.store import save_observations


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


def test_exact_bytes_encoding_reparse_and_typed_idempotency(tmp_path, connect):
    body = (monex_html() + "<!-- 日本語の元本文 -->").encode("cp932")
    session = Session([(200, body, "cp932")])
    connect(session)
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    saved = stage(store, p)
    metadata, recovered = external_assessment_raw.read_raw_bundle(store, saved.raw_bundle_keys[0])
    assert recovered == (body,)
    receipt = metadata["receipts"][0]
    assert receipt["encoding"] == "cp932"
    assert receipt["url"] == monex.public_url("7203")
    observation = monex.parse_html(
        recovered[0].decode(receipt["encoding"]),
        ticker="7203",
        source_url=receipt["url"],
        fetched_at_utc=datetime.fromisoformat(receipt["fetched_at_utc"]),
    )
    assert saved.result.observations == (observation,)
    path = tmp_path / "observations.sqlite3"
    assert save_observations(path, [observation]) == 1
    assert save_observations(path, [observation]) == 0
    manifest_bytes = json.dumps(metadata).encode()
    assert b"private-cookie-header" not in manifest_bytes
    assert b"private-authorization-header" not in manifest_bytes
    assert len(store.puts) == 2
    assert store.puts[0][0].endswith("cohort-0000.json")
    assert store.puts[1][0] == saved.raw_bundle_keys[0]


def test_rakuten_outer_and_iframe_bytes_are_in_one_archive(connect):
    outer, iframe = official_html().encode(), rakuten_html().encode()
    session = Session([(200, outer, "utf-8"), (200, iframe, "utf-8")])
    connect(session)
    p, store = plan(sources=("rakuten",)), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    saved = stage(store, p)
    manifest, bodies = external_assessment_raw.read_raw_bundle(store, saved.raw_bundle_keys[0])
    assert bodies == (outer, iframe)
    receipt = manifest["receipts"][1]
    parsed = rakuten.parse_html(
        bodies[1].decode(),
        ticker="7203",
        source_url=receipt["url"],
        fetched_at_utc=datetime.fromisoformat(receipt["fetched_at_utc"]),
    )
    assert saved.result.observations == (parsed,)
    assert len(store.puts) == 2


@pytest.mark.parametrize("status", [403, 404, 500])
def test_non200_has_no_raw_bundle(connect, status):
    connect(Session([(status, b"denied", "utf-8")]))
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    assert stage(store, p).result.entries[0].state == "failure"
    assert stage(store, p).raw_bundle_keys == ()
    assert bundles(store) == []


@pytest.mark.parametrize(
    ("html", "state"), [("invalid public HTML", "failure"), (monex_html(value="--"), "normal_null")]
)
def test_parse_failure_and_normal_null_preserve_diagnostic_bytes(connect, html, state):
    connect(Session([(200, html.encode(), "utf-8")]))
    p, store = plan(), FakeObjectStore()
    daily.acquire_cohort(store, p, 0)
    saved = stage(store, p)
    assert saved.result.entries[0].state == state
    assert external_assessment_raw.read_raw_bundle(store, saved.raw_bundle_keys[0])[1] == (
        html.encode(),
    )


@pytest.mark.parametrize("fault", ["before", "after", "bad_receipt"])
def test_raw_upload_failure_warns_and_primary_stage_remains_complete(connect, capsys, fault):
    connect(Session([(200, monex_html().encode(), "utf-8")]))
    p, store = plan(), FakeObjectStore()
    store.put_fault = (2, fault)
    assert daily.acquire_cohort(store, p, 0)["cohort_complete"] is True
    saved = stage(store, p)
    assert saved.result.entries[0].state == "success"
    assert "diagnostic raw save failed" in capsys.readouterr().err
    before = list(store.puts)
    assert daily.acquire_cohort(store, p, 0)["provider_requests"] == 0
    assert store.puts == before


@pytest.mark.parametrize("method", ["append", "finish"])
def test_local_raw_failure_cannot_discard_typed_success(connect, monkeypatch, capsys, method):
    connect(Session([(200, monex_html().encode(), "utf-8")]))

    def fail(*a, **k):
        if method == "finish":
            assert stage(store, p).result.entries[0].state == "success"
        raise OSError("private secret must not be logged")

    monkeypatch.setattr(external_assessment_raw.RawBundle, method, fail)
    p, store = plan(), FakeObjectStore()
    assert daily.acquire_cohort(store, p, 0)["cohort_complete"] is True
    assert stage(store, p).result.entries[0].state == "success"
    assert len(store.puts) == 1
    error = capsys.readouterr().err
    assert "diagnostic raw save failed" in error
    assert "private secret" not in error


def test_raw_upload_follows_typed_checkpoint_and_never_downloads_per_body(connect, monkeypatch):
    connect(Session([(200, monex_html().encode(), "utf-8")]))
    p, store = plan(), FakeObjectStore()
    original = store.put_file

    def put(key, path, **kwargs):
        if key.startswith(external_assessment_raw.RAW_PREFIX):
            assert stage(store, p).result.entries[0].state == "success"
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
            for source, body in zip(
                ("monex", "rakuten", "rakuten", "kabumap", *("rakuten",) * 5),
                pages + [pages[1]] * 5,
                strict=True,
            ):
                expected.append(body)
                bundle.append(
                    source,
                    ticker,
                    ExternalAssessmentReceivedHTML(
                        "https://synthetic.invalid/public",
                        datetime(2026, 10, 11, tzinfo=UTC),
                        "utf-8",
                        body,
                    ),
                )
        key = bundle.finish()
        bundle.upload(store)
        archive_bytes = bundle.path.stat().st_size
        raw_bytes = sum(map(len, expected))
        assert archive_bytes < raw_bytes
        assert len(store.puts) == 1
        manifest, restored = external_assessment_raw.read_raw_bundle(store, key)
        assert len(manifest["receipts"]) == 450
        assert restored == tuple(expected)


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
