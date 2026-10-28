"""A job cutoff stops reads without shortening Retry-After or persisting state."""

import io

import pytest
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.response import HTTPResponse

from baibai_engine.data_collection.transport.retry import ReadDeadline
from baibai_engine.data_collection.providers.jpx.http import JPXHTTP
from baibai_engine.data_collection.providers.jpx.models import JPXAcquisitionError
from baibai_engine.data_collection.transport.http import (
    AcquisitionDeadlineExceeded,
    check_acquisition_deadline,
    wait_for_read_retry,
)


@pytest.mark.parametrize("deadline", ["100", "99", "nan", "inf", "invalid"])
def test_expired_or_invalid_job_cutoff_stops(deadline, monkeypatch):
    monkeypatch.setenv("BAIBAI_ACQUISITION_DEADLINE", deadline)
    monkeypatch.setattr("time.time", lambda: 100.0)
    with pytest.raises(AcquisitionDeadlineExceeded):
        check_acquisition_deadline()


def test_wait_must_fit_without_shortening_and_a_new_job_has_no_old_state(monkeypatch):
    monkeypatch.setattr("time.time", lambda: 100.0)
    waits = []
    monkeypatch.setattr("time.sleep", waits.append)
    monkeypatch.setenv("BAIBAI_ACQUISITION_DEADLINE", "110")
    with pytest.raises(AcquisitionDeadlineExceeded):
        wait_for_read_retry(10)
    assert waits == []
    wait_for_read_retry(9)
    assert waits == [9]
    monkeypatch.delenv("BAIBAI_ACQUISITION_DEADLINE")
    wait_for_read_retry(20)
    assert waits == [9, 20]


def test_economic_provider_budget_cannot_extend_the_job_cutoff(monkeypatch):
    monkeypatch.setenv("BAIBAI_ACQUISITION_DEADLINE", "110")
    monkeypatch.setattr("time.time", lambda: 100.0)
    deadline = ReadDeadline("synthetic", seconds=1200)
    with pytest.raises(AcquisitionDeadlineExceeded):
        deadline.check(10)


@pytest.mark.parametrize("status", [429, 403])
def test_jpx_real_transport_has_finite_status_retries(status, monkeypatch):
    calls = []
    waits = []

    def respond(*args, **kwargs):
        calls.append(1)
        return HTTPResponse(
            body=io.BytesIO(b"response"),
            status=status,
            headers={"Retry-After": "1"},
            preload_content=False,
        )

    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(HTTPSConnectionPool, "_make_request", respond)
    monkeypatch.setattr("time.sleep", waits.append)
    monkeypatch.setattr("socket.socket.connect", lambda *args: pytest.fail("unexpected network"))
    client = JPXHTTP()
    try:
        with pytest.raises(JPXAcquisitionError):
            client.fetch("https://www.jpx.co.jp/listing/alert/index.html")
    finally:
        client.close()
    assert len(calls) == (6 if status == 429 else 1)
    assert waits == ([1] * 5 if status == 429 else [])


def test_jpx_stops_before_a_request_when_job_is_expired(monkeypatch):
    monkeypatch.setenv("BAIBAI_ACQUISITION_DEADLINE", "0")
    client = JPXHTTP()
    try:
        with pytest.raises(AcquisitionDeadlineExceeded):
            client.fetch("https://www.jpx.co.jp/listing/alert/index.html")
    finally:
        client.close()
