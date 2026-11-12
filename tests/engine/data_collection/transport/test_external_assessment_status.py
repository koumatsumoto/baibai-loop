"""Actual requests streaming keeps final status ahead of an unreadable error body."""

import io
from datetime import timedelta

import pytest
import requests
from tests.helpers.external_assessment_html import monex_html
from tests.helpers.external_assessment_http import HostRateClock
from tests.helpers.external_assessment_stages import FakeObjectStore, plan, stage

from baibai_engine.data_collection.transport.public_pages import HostRateLimiter
from baibai_jobs.acquisition.external_assessments import collection, daily
from baibai_jobs.acquisition.external_assessments.raw import read_raw_bundle


class Response(requests.Response):
    def __init__(self, status, body=None):
        super().__init__()
        self.status_code = status
        self.body_reads = self.closes = 0
        self.raw = io.BytesIO(b"")
        if body is not None:
            self._content = body
            self._content_consumed = True
        self.headers.update({"Content-Type": "text/html; charset=utf-8", "Retry-After": "86400"})

    @property
    def content(self):
        self.body_reads += 1
        if self._content is False:
            raise requests.ReadTimeout("private synthetic error body")
        return self._content

    def close(self):
        self.closes += 1
        super().close()


class Adapter(requests.adapters.HTTPAdapter):
    """No network; Session.send still applies requests' real stream=False body read."""

    def __init__(self, responses):
        super().__init__()
        self.responses = iter(responses)
        self.calls = []

    def send(self, request, **kwargs):
        self.calls.append((request.url, kwargs["stream"]))
        response = next(self.responses)
        response.request = request
        return response


@pytest.fixture
def clock(monkeypatch):
    value = HostRateClock()
    monkeypatch.setattr(collection.time, "monotonic", value.monotonic)
    monkeypatch.setattr(collection.time, "sleep", value.sleep)
    return value


@pytest.mark.parametrize("source", ["monex", "rakuten", "kabumap", "minkabu"])
@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.parametrize("paired", [False, True])
def test_status_stops_direct_and_pair_collection_before_error_body(clock, source, status, paired):
    refusal = Response(status)
    adapter = Adapter([refusal])
    with requests.Session() as client:
        client.mount("https://", adapter)
        if paired:
            result = collection.collect_pair(
                "7203", source=source, session=client, rate_limiter=HostRateLimiter()
            )
            assert result.stop_reason == ("rate_limited" if status == 429 else "access_denied")
            if status == 429:
                assert result.retry_after_not_before_utc == (
                    result.last_response_finished_at_utc + timedelta(days=1)
                )
        else:
            result = collection.collect(
                ["7203", "9432"], sources=(source,), session=client, request_budget=3
            )
            assert result.outcomes[1].status == "not_attempted"
    assert result.outcomes[0].http_status == status
    assert result.requests == len(adapter.calls) == 1
    assert adapter.calls[0][1] is True
    assert refusal.body_reads == 0
    assert refusal.closes == 1


@pytest.mark.parametrize("status", [401, 403, 429])
def test_cohort_keeps_prior_raw_and_stops_next_ticker_on_unreadable_refusal(clock, status):
    body = monex_html("7203").encode()
    healthy, refusal = Response(200, body), Response(status)
    adapter = Adapter([healthy, refusal])
    p, store = plan(tickers=("7203", "8306", "9432")), FakeObjectStore()
    with requests.Session() as client:
        client.mount("https://", adapter)
        report = daily.acquire_cohort(store, p, 0, session=client)
    saved = stage(store, p)
    assert report["provider_requests"] == len(adapter.calls) == 2
    assert [entry.state for entry in saved.result.entries] == [
        "success",
        "failure",
        "unattempted",
    ]
    assert saved.result.entries[1].http_status == status
    assert report["response_content_bytes"] == len(body)
    _metadata, originals = read_raw_bundle(store, saved.raw_bundle_keys[0])
    assert len(originals) == 1
    assert originals[0].body == body
    assert originals[0].attempt_id == saved.result.entries[0].attempt_id
    assert refusal.body_reads == 0
    assert refusal.closes == healthy.closes == 1


def test_unreadable_success_body_is_still_a_transport_failure(clock):
    failed = Response(200)
    adapter = Adapter([failed])
    with requests.Session() as client:
        client.mount("https://", adapter)
        result = collection.collect_pair(
            "7203", source="monex", session=client, rate_limiter=HostRateLimiter()
        )
    assert result.outcomes[0].status == "request_timeout"
    assert result.outcomes[0].http_status is None
    assert result.observations == ()
    assert result.response_content_bytes == 0
    assert failed.body_reads == failed.closes == 1
