"""外部評価HTTPの異なる応答・時計・原本保存契約を試す合成入力。

bytes受領、host待機、pair途中応答、raw bundleのheader秘匿は異なるfixtureで保持する。
実providerへ通信するclientではない。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import requests

import baibai_jobs.acquisition.external_assessments.collection as collection
import baibai_jobs.acquisition.external_assessments.daily as daily
from baibai_engine.data_collection.receipts.public_pages.models import EquityWebSourceResponse
from baibai_engine.machine_analysis.observations.external_assessments.normalization import monex
from tests.helpers.external_assessment_html import monex_html


class CaptureSession:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        status, html = next(self.replies)
        response = requests.Response()
        response.status_code = status
        response._content = html.encode()
        response._content_consumed = True
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return response


class BytesSession(CaptureSession):
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        status, body, headers = next(self.replies)
        response = requests.Response()
        response.status_code = status
        response._content = body
        response._content_consumed = True
        response.headers.update(headers)
        return response


def _original(*, attempt_id=None, body=None):
    return EquityWebSourceResponse(
        receipt_id=str(uuid.uuid4()),
        attempt_id=attempt_id or str(uuid.uuid4()),
        source="monex",
        requested_ticker="7203",
        role="page",
        parent_receipt_id=None,
        requested_url=monex.public_url("7203"),
        received_at_utc=datetime(2024, 1, 1, tzinfo=UTC),
        selected_encoding="utf-8",
        body=body if body is not None else monex_html().encode(),
    )


class HostRateClock:
    def __init__(self):
        self.seconds = 100.0

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds


class HostRateSession(requests.Session):
    def __init__(self, clock, replies=None):
        super().__init__()
        self.clock = clock
        self.replies = iter(replies) if replies is not None else None
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((self.clock.seconds, url, kwargs))
        status, html, headers = (
            next(self.replies)
            if self.replies is not None
            else (200, monex_html(url.rsplit("/", 1)[-1]), {})
        )
        response = requests.Response()
        response.status_code = status
        response._content = html.encode()
        response._content_consumed = True
        response.headers.update(headers)
        return response


class PairSession(requests.Session):
    def __init__(self, replies, *, clock=None, events=None):
        super().__init__()
        self.replies = iter(replies)
        self.calls = []
        self.clock = clock
        self.events = events
        self.finished = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs, self.clock.now if self.clock is not None else None))
        if self.events is not None:
            assert self.events[-1] == "before"
            self.events.append("request")
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        status, html = reply
        if self.clock is not None:
            self.clock.now += 0.25
            self.finished.append(self.clock.now)
        response = requests.Response()
        response.status_code = status
        response._content = html.encode()
        response._content_consumed = True
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return response


class RawSession(requests.Session):
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
