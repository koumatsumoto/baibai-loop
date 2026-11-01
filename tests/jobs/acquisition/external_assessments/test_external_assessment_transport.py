"""Private PUT acknowledgements preserve durable acquisition with fewer R2 trips."""

from __future__ import annotations

import base64
import hashlib
import io
import uuid
from collections import Counter

import pytest
import requests
from botocore.exceptions import ClientError, ReadTimeoutError
from tests.helpers.external_assessment_html import monex_html
from tests.helpers.external_assessment_responses import response
from tests.helpers.external_assessment_stages import FakeObjectStore, observation, plan

import baibai_jobs.acquisition.external_assessments.daily as daily
import baibai_jobs.acquisition.external_assessments.raw as external_assessment_raw
import baibai_jobs.acquisition.external_assessments.staging as external_assessment_staging
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    LakeCASConflict,
    R2Error,
)
from baibai_jobs.acquisition.external_assessments.collection import (
    CollectionOutcome as ExternalAssessmentCollectionOutcome,
)
from baibai_jobs.acquisition.external_assessments.collection import (
    CollectionResult as ExternalAssessmentCollectionResult,
)


class S3Client:
    def __init__(self, clock):
        self.clock = clock
        self.objects = {}
        self.calls = Counter()
        self.put_fault = None
        self.puts = 0

    def tick(self, operation):
        self.calls[operation] += 1
        self.clock[0] += 0.1  # Fixed simulated R2 latency; no network.

    def head_object(self, *, Key, **kwargs):
        self.tick("HEAD")
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        body, metadata, content_type, etag = self.objects[Key]
        return {
            "ETag": etag,
            "ContentLength": len(body),
            "Metadata": metadata,
            "ContentType": content_type,
        }

    def get_object(self, *, Key, **kwargs):
        self.tick("GET")
        return {"Body": io.BytesIO(self.objects[Key][0])}

    def put_object(self, *, Key, Body, Metadata, ContentType, ContentMD5, **kwargs):
        self.tick("PUT")
        body = Body.read()
        assert hashlib.sha256(body).hexdigest() == Metadata["sha256"]
        assert (
            base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode()
            == ContentMD5
        )
        self.puts += 1
        if self.put_fault == (self.puts, "conflict"):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        prior = self.objects.get(Key)
        if kwargs.get("IfNoneMatch") == "*":
            assert prior is None
        else:
            assert prior is not None
            assert kwargs["IfMatch"] == prior[3].strip('"')
        etag = f'"opaque-generation-{self.puts}"'
        self.objects[Key] = body, Metadata, ContentType, etag
        if self.put_fault == (self.puts, "lost_response"):
            raise ReadTimeoutError(endpoint_url="https://synthetic.invalid")
        return {} if self.put_fault == (self.puts, "missing_generation") else {"ETag": etag}


def store_and_client(monkeypatch, clock, *, verify_put_with_head):
    store = Boto3R2Store(
        bucket="synthetic",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "synthetic",
            "R2_SECRET_ACCESS_KEY": "synthetic",
        },
        verify_put_with_head=verify_put_with_head,
    )
    client = S3Client(clock)
    monkeypatch.setattr(store, "_client", lambda *_: client)
    return store, client


def collector(monkeypatch, clock, calls):
    def collect(ticker, *, source, on_received_html, **kwargs):
        calls.append(ticker)
        clock[0] += 0.05  # Fixed simulated provider receive/parse latency.
        received = observation(ticker).fetched_at_utc
        body = ("public " + ticker).encode()
        attempt_id = str(uuid.uuid4())
        on_received_html(
            response(source, ticker, body, attempt_id=attempt_id, received_at_utc=received)
        )
        return ExternalAssessmentCollectionResult(
            (observation(ticker),),
            (
                ExternalAssessmentCollectionOutcome(
                    source, ticker, "observed", http_status=200, attempt_id=attempt_id
                ),
            ),
            1,
        )

    monkeypatch.setattr(daily, "external_assessment_collect_pair", collect)


def test_acknowledged_put_reduces_round_trips_without_missing_or_duplicate_receipts(monkeypatch):
    p = plan(tickers=tuple(str(t) for t in range(1301, 1351)))
    runs = []
    for verify in (True, False):
        clock, calls = [0.0], []
        monkeypatch.setattr(daily.time, "monotonic", lambda clock=clock: clock[0])
        collector(monkeypatch, clock, calls)
        store, client = store_and_client(monkeypatch, clock, verify_put_with_head=verify)
        report = daily.acquire_cohort(store, p, 0)
        elapsed, operations = clock[0], client.calls.copy()
        assert report["cohort_complete"] is True
        assert report["provider_requests"] == len(calls) == 50
        saved = external_assessment_staging.read_private(
            store, external_assessment_staging.stage_key(p, 0)
        )
        durable = external_assessment_staging.load_stage(p, saved.payload)
        assert len(durable.result.observations) == 50
        metadata, bodies = external_assessment_raw.read_raw_bundle(
            store, durable.raw_bundle_keys[0]
        )
        assert tuple(item.body for item in bodies) == tuple(
            ("public " + ticker).encode() for ticker in p.tickers
        )
        assert [item["requested_ticker"] for item in metadata["receipts"]] == list(p.tickers)
        before = client.calls["PUT"]
        resumed = daily.acquire_cohort(store, p, 0)
        assert resumed["collection_elapsed_seconds"] == 0
        assert client.calls["PUT"] == before
        assert len(calls) == 50
        runs.append((elapsed, operations, durable, report))
    slow, fast = runs
    # Both modes read the cross-run source quota before any provider request.
    assert slow[1] == {"HEAD": 5, "PUT": 2}
    assert fast[1] == {"HEAD": 3, "PUT": 2}
    assert fast[2].result.observations == slow[2].result.observations
    assert slow[0] == pytest.approx(3.2)
    assert fast[0] == pytest.approx(3.0)
    assert fast[3]["storage_elapsed_seconds"] == pytest.approx(0.5)
    assert slow[3]["storage_elapsed_seconds"] == pytest.approx(0.7)


@pytest.mark.parametrize("fault", ["conflict", "lost_response", "missing_generation"])
def test_ambiguous_stage_acknowledgement_reads_actual_result(monkeypatch, fault):
    clock, calls = [0.0], []
    monkeypatch.setattr(daily.time, "monotonic", lambda: clock[0])
    collector(monkeypatch, clock, calls)
    store, client = store_and_client(monkeypatch, clock, verify_put_with_head=False)
    client.put_fault = 2, fault
    if fault == "conflict":
        with pytest.raises(RuntimeError, match="conditional"):
            daily.acquire_cohort(store, plan(tickers=("7203", "9432")), 0)
    else:
        assert (
            daily.acquire_cohort(store, plan(tickers=("7203", "9432")), 0)["cohort_complete"]
            is True
        )
    assert calls == ["7203", "9432"]
    assert client.calls["GET"] == (0 if fault == "conflict" else 1)


@pytest.mark.parametrize("fault", ["conflict", "lost_response", "missing_generation"])
def test_failed_bundle_acknowledgement_prevents_stage_commit(monkeypatch, capsys, fault):
    clock, calls = [0.0], []
    monkeypatch.setattr(daily.time, "monotonic", lambda: clock[0])
    collector(monkeypatch, clock, calls)
    store, client = store_and_client(monkeypatch, clock, verify_put_with_head=False)
    client.put_fault = 1, fault
    p = plan()
    with pytest.raises(LakeCASConflict if fault == "conflict" else R2Error):
        daily.acquire_cohort(store, p, 0)
    assert external_assessment_staging.read_stage(store, p, 0) is None
    output = capsys.readouterr()
    assert "synthetic.invalid" not in output.err


def test_source_cli_only_disables_put_head_for_private_acquisition(monkeypatch, tmp_path):
    import baibai_jobs.acquisition.external_assessments.sources as sources

    configured = []
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **kwargs: configured.append(kwargs))
    monkeypatch.setattr(daily, "initialize", lambda *args, **kwargs: plan())
    monkeypatch.setattr(
        sources, "acquire_source", lambda *args, **kwargs: {"status": "already_published"}
    )
    assert (
        sources.main(
            [
                "acquire",
                "--source",
                "monex",
                "--run-id",
                "monex-synthetic",
                "--master-asof",
                "2026-10-11",
                "--directory",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert configured == [{"bucket": "baibai-stores", "verify_put_with_head": False}]


@pytest.mark.parametrize("http_status", [200, 403])
def test_cohort_reuses_provider_connections_and_resets_pair_cookies(monkeypatch, http_status):
    clients = []
    clock = [0.0]
    monkeypatch.setattr(daily.time, "monotonic", lambda clock=clock: clock[0])
    monkeypatch.setattr(
        daily.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    class ProviderSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.calls = []
            self.closed = 0
            clients.append(self)

        def get(self, url, **kwargs):
            assert not self.cookies
            ticker = ("7203", "9432")[len(self.calls)]
            self.calls.append(url)
            self.cookies.set("pair_cookie", "public-state")
            response = requests.Response()
            response.status_code = http_status
            response._content = monex_html(ticker).encode()
            response._content_consumed = True
            response.headers["Content-Type"] = "text/html; charset=utf-8"
            return response

        def close(self):
            self.closed += 1
            super().close()

    monkeypatch.setattr(daily.requests, "Session", ProviderSession)
    report = daily.acquire_cohort(FakeObjectStore(), plan(tickers=("7203", "9432")), 0)
    assert report["cohort_complete"] is (http_status == 200)
    assert len(clients) == 1
    assert len(clients[0].calls) == (2 if http_status == 200 else 1)
    assert clients[0].closed == 1
