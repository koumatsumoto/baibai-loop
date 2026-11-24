"""Use the exact SDK signing/response path; the fixture runs no network."""

from contextlib import closing
from dataclasses import replace
from io import BytesIO

import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ReadTimeoutError
from botocore.httpsession import URLLib3Session
from tests.helpers.lake_release import lake as lake
from tests.jobs.publication.test_machine_current import HEAD, _candidate
from tests.jobs.publication.test_machine_current import incumbent as incumbent

from baibai_engine.storage.objects.r2 import Boto3R2Store, R2Error, R2OperationStopped
from baibai_engine.storage.snapshots.models import canonical_lake_model_bytes
from baibai_jobs.publication import machine_current as machine

pytestmark = pytest.mark.usefixtures("lake")


class _Raw(BytesIO):
    def stream(self, *_args, **_kwargs):
        yield self.read()


@pytest.mark.parametrize(
    ("status", "code", "region"),
    [
        (301, "PermanentRedirect", "us-west-2"),
        (301, "PermanentRedirect", None),
        (400, "AuthorizationHeaderMalformed", "us-west-2"),
        (500, "InternalError", None),
        (401, "AccessDenied", None),
        (403, "AuthorizationHeaderMalformed", "us-west-2"),
        (429, "AuthorizationHeaderMalformed", "us-west-2"),
        (None, None, None),
        (200, None, None),
    ],
)
def test_machine_head_cas_sends_only_one_signed_put(
    incumbent, tmp_path, monkeypatch, status, code, region
):
    fake, _, base = incumbent
    staged = _candidate(fake, tmp_path, "next")
    payload = canonical_lake_model_bytes(staged.pointer)
    calls = []
    acked = False

    def send(_session, request):
        nonlocal acked
        calls.append(request.method)
        auth = request.headers["Authorization"]
        assert b"AWS4-HMAC-SHA256" in auth
        if request.method == "HEAD":
            # An unguarded SDK can attempt this independent bucket discovery.
            return AWSResponse(request.url, 200, {"x-amz-bucket-region": "us-west-2"}, _Raw())
        assert request.method == "PUT"
        assert request.url.endswith(f"/{HEAD}")
        assert request.headers["If-Match"] == f'"{base.precondition.etag}"'.encode()
        assert b"if-match" in auth
        if calls.count("PUT") > 1 or status == 200:
            acked = True
            return AWSResponse(request.url, 200, {"etag": '"next"'}, _Raw())
        if status is None:
            raise ReadTimeoutError(endpoint_url=request.url)
        headers = {"content-type": "application/xml"}
        if region is not None:
            headers["x-amz-bucket-region"] = region
        detail = f"<Region>{region}</Region>" if region else ""
        return AWSResponse(
            request.url,
            status,
            headers,
            _Raw(f"<Error><Code>{code}</Code>{detail}</Error>".encode()),
        )

    monkeypatch.setattr(URLLib3Session, "send", send)
    # Independent fixtures prove these guards; isolate the real write SDK from
    # pre- and postcondition reader calls so discovery cannot blend into them.
    monkeypatch.setattr(machine, "_require_staged_lake", lambda *_: None)
    monkeypatch.setattr(machine, "_require_members", lambda *_: None)

    def readback(_):
        assert status not in {401, 403, 429}, "terminal rejection must stop before reconcile"
        return replace(base, pointer=staged.pointer, payload=payload) if acked else base

    monkeypatch.setattr(machine, "read_pointer_snapshot", readback)
    with closing(
        Boto3R2Store(
            bucket="test-vessel",
            env={
                "R2_ACCOUNT_ID": "a" * 32,
                "R2_ACCESS_KEY_ID": "local-fixture",
                "R2_SECRET_ACCESS_KEY": "local-fixture",
                "R2_STOP_ON_REJECTION": "1",
            },
            verify_put_with_head=False,
        )
    ) as store:
        if status == 200:
            assert (
                machine.activate_machine(store, staged, before_switch=lambda: None).current.payload
                == payload
            )
        else:
            error = (
                R2OperationStopped
                if status in {403, 429}
                else R2Error
                if status == 401
                else machine.MachineSwitchUnconfirmed
            )
            with pytest.raises(error):
                machine.activate_machine(store, staged, before_switch=lambda: None)
    assert calls == ["PUT"]
