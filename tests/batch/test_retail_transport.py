"""Private PUT acknowledgements preserve durable acquisition with fewer R2 trips."""

from __future__ import annotations

import base64
import hashlib
import io
from collections import Counter

import pytest
import requests
from botocore.exceptions import ClientError, ReadTimeoutError
from tests.batch.test_retail_daily import FakeObjectStore, observation, plan
from tests.engine.test_retail_dynamics_sources import monex_html

from baibai_batch.jobs import retail_daily as daily
from baibai_batch.storage import retail_raw, retail_staging
from baibai_batch.storage.lake_publish import Boto3R2Store
from baibai_engine.batch_api import (
    RetailCollectionOutcome,
    RetailCollectionResult,
    RetailReceivedHTML,
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
    def collect(
        ticker, *, source, before_request, on_outcome, on_response, on_received_html, **kwargs
    ):
        before_request()
        calls.append(ticker)
        clock[0] += 0.05  # Fixed simulated provider receive/parse latency.
        received = observation(ticker).fetched_at_utc
        body = ("public " + ticker).encode()
        on_response(received, 1, len(body))
        on_received_html(
            RetailReceivedHTML("https://synthetic.invalid/" + ticker, received, "utf-8", body)
        )
        result = RetailCollectionResult(
            (observation(ticker),),
            (RetailCollectionOutcome(source, ticker, "observed", http_status=200),),
            1,
        )
        on_outcome(result)
        return result

    monkeypatch.setattr(daily, "retail_collect_pair", collect)


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
        assert report["confirmed_http_requests"] == len(calls) == 50
        saved = retail_staging.read_private(store, retail_staging.checkpoint_key(p, 0))
        durable = retail_staging.load_checkpoint(p, saved.payload)
        assert len(durable.journal.observations) == 50
        for number, ticker in enumerate(p.tickers, 1):
            key = f"retail-dynamics/raw/utc-date/2026-10-11/receipts/{p.run_id}/monex/{ticker}/{number}.json"
            metadata, body = retail_raw.read_raw_html(store, key)
            assert body == ("public " + ticker).encode()
            assert metadata["requested_ticker"] == ticker
        before = client.calls["PUT"]
        resumed = daily.acquire_cohort(store, p, 0)
        assert resumed["collection_elapsed_seconds"] == 0
        assert client.calls["PUT"] == before
        assert len(calls) == 50
        runs.append((elapsed, operations, durable, report))
    slow, fast = runs
    assert slow[1] == {"HEAD": 302, "PUT": 200}
    assert fast[1] == {"HEAD": 102, "PUT": 200}
    assert fast[2] == slow[2]
    assert slow[0] == pytest.approx(52.7)
    assert fast[0] == pytest.approx(32.7)
    assert fast[3]["storage_elapsed_seconds"] == pytest.approx(30.2)
    assert slow[3]["storage_elapsed_seconds"] == pytest.approx(50.2)


@pytest.mark.parametrize("put_number", [1, 2])
@pytest.mark.parametrize("fault", ["conflict", "lost_response", "missing_generation"])
def test_failed_private_put_acknowledgement_stops_before_next_http(monkeypatch, put_number, fault):
    clock, calls = [0.0], []
    monkeypatch.setattr(daily.time, "monotonic", lambda clock=clock: clock[0])
    collector(monkeypatch, clock, calls)
    store, client = store_and_client(monkeypatch, clock, verify_put_with_head=False)
    client.put_fault = put_number, fault
    with pytest.raises(RuntimeError):
        daily.acquire_cohort(store, plan(tickers=("7203", "9432")), 0)
    assert calls == ([] if put_number == 1 else ["7203"])
    assert client.calls["HEAD"] == 2


@pytest.mark.parametrize("put_number", [3, 4])
@pytest.mark.parametrize("fault", ["conflict", "lost_response", "missing_generation"])
def test_failed_raw_put_acknowledgement_keeps_confirmed_typed_result(
    monkeypatch, capsys, put_number, fault
):
    clock, calls = [0.0], []
    monkeypatch.setattr(daily.time, "monotonic", lambda: clock[0])
    collector(monkeypatch, clock, calls)
    store, client = store_and_client(monkeypatch, clock, verify_put_with_head=False)
    client.put_fault = put_number, fault
    p = plan()
    report = daily.acquire_cohort(store, p, 0)
    assert report["cohort_complete"] is True
    assert report["confirmed_http_requests"] == 1
    saved = retail_staging.read_private(store, retail_staging.checkpoint_key(p, 0))
    assert retail_staging.load_checkpoint(p, saved.payload).journal.observations == (observation(),)
    output = capsys.readouterr()
    assert "typed result unchanged" in output.err
    assert "synthetic.invalid" not in output.err


def test_cli_only_disables_put_head_for_private_acquisition(monkeypatch):
    configured = []
    monkeypatch.setattr(daily, "Boto3R2Store", lambda **kwargs: configured.append(kwargs))
    monkeypatch.setattr(daily, "read_plan", lambda *args: plan())
    monkeypatch.setattr(daily, "acquire_cohort", lambda *args: {"status": "already_published"})
    assert daily.main(["acquire", "--run-id", "synthetic", "--cohort", "0"]) == 0
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
