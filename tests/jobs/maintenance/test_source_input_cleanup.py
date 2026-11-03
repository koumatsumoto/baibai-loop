import hashlib
import io
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from baibai_jobs.maintenance.source_input_cleanup import run


class Fake:
    def __init__(self):
        events = SimpleNamespace(handler=None)
        events.register_first = lambda _event, handler: setattr(events, "handler", handler)
        events.unregister = lambda _event, _handler: setattr(events, "handler", None)
        self.meta = SimpleNamespace(events=events)
        self.payload = b"original"
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.pointer = "coordination/source-inputs/group.json"
        self.stage = "group-current"
        self.keep = f"coordination/source-inputs/group-old1/receipts-{self.sha}.sqlite"
        self.old = f"coordination/source-inputs/group-old2/receipts-{self.sha}.sqlite"
        self.objects = {self.keep: self.payload, self.old: self.payload}
        self.deletes = []

    def get_paginator(self, _name):
        return self

    def paginate(self, **_kwargs):
        return [{"Contents": [{"Key": self.pointer}]}]

    def get_object(self, **kwargs):
        key = kwargs["Key"]
        if key == self.pointer:
            return {
                "ETag": '"pointer"',
                "Body": io.BytesIO(json.dumps({"stage_id": self.stage}).encode()),
            }
        return {"ETag": '"object"', "Body": io.BytesIO(self.objects[key])}

    def head_object(self, **kwargs):
        if kwargs["Key"] not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )
        data = self.objects[kwargs["Key"]]
        return {"ETag": '"object"', "ContentLength": len(data), "Metadata": {"sha256": self.sha}}

    def delete_object(self, **kwargs):
        assert "IfMatch" not in kwargs
        self.meta.events.handler()
        self.deletes.append(kwargs["Key"])
        del self.objects[kwargs["Key"]]

    def plan(self):
        def item(k):
            return {"key": k, "sha256": self.sha, "size": len(self.payload), "etag": '"object"'}

        return {
            "bucket": "baibai-stores",
            "account_endpoint_sha256": "a" * 64,
            "retired_handoffs": {},
            "pointers": {self.pointer: ['"pointer"', self.stage]},
            "groups": [{"keep": item(self.keep), "delete": [item(self.old)]}],
        }


def test_dry_run_has_no_deletes_and_apply_requires_guard(monkeypatch):
    s, plan = _retired_fixture(monkeypatch)
    assert run(s, plan)["objects"] == 1
    assert not s.deletes
    with pytest.raises(RuntimeError, match="apply requires"):
        run(s, plan, apply=True)
    assert s.keep in s.objects
    assert s.old in s.objects


def test_stale_pointer_prevents_any_deletion():
    s = Fake()
    p = s.plan()
    s.stage = "new-stage"
    with pytest.raises(ValueError, match="pointers changed"):
        run(s, p)
    assert not s.deletes


def test_target_current_stage_is_never_deleted():
    s = Fake()
    p = s.plan()
    s.stage = "group-old2"
    p["pointers"][s.pointer][1] = s.stage
    with pytest.raises(ValueError, match="current or invalid"):
        run(s, p)
    assert not s.deletes


def test_missing_or_corrupt_survivor_fails_before_any_delete(monkeypatch):
    s, p = _retired_fixture(monkeypatch)
    s.objects[s.keep] = b"changed!"
    with pytest.raises(ValueError, match="survivor bytes"):
        run(s, p)
    assert not s.deletes


def test_scope_and_duplicate_mismatch_fail_closed():
    s = Fake()
    p = s.plan()
    p["groups"][0]["delete"][0]["size"] += 1
    with pytest.raises(ValueError, match="duplicate identity"):
        run(s, p)
    p = deepcopy(s.plan())
    p["bucket"] = "different"
    with pytest.raises(ValueError, match="scope"):
        run(s, p)
    assert not s.deletes


def test_duplicate_json_members_are_rejected():
    from baibai_jobs.maintenance.source_input_cleanup import _unique_members

    with pytest.raises(ValueError, match="duplicate JSON"):
        json.loads('{"groups": [], "groups": []}', object_pairs_hook=_unique_members)


@pytest.mark.parametrize("size", [True, 1.0, None, 0, -1])
def test_invalid_sizes_are_rejected(size):
    s = Fake()
    p = s.plan()
    p["groups"][0]["delete"][0]["size"] = size
    with pytest.raises(ValueError, match="invalid or repeated"):
        run(s, p)
    assert not s.deletes


def test_completed_handoff_pins_old_receipt(monkeypatch):
    s = Fake()
    p = s.plan()
    handoff_key = s.old.rsplit("/", 1)[0] + "/handoff.json"
    paginate = s.paginate
    get_object = s.get_object

    def pages(**kwargs):
        result = paginate(**kwargs)
        result[0]["Contents"].append({"Key": handoff_key})
        return result

    def read(**kwargs):
        if kwargs["Key"] == handoff_key:
            return {"Body": io.BytesIO(json.dumps({"receipt_key": s.old}).encode())}
        return get_object(**kwargs)

    monkeypatch.setattr(s, "paginate", pages)
    monkeypatch.setattr(s, "get_object", read)
    with pytest.raises(ValueError, match="handoff reference differs"):
        run(s, p)
    assert not s.deletes


@pytest.mark.parametrize("bad", [None, {}, {"key": []}])
def test_invalid_receipt_entries_fail_cleanly(bad):
    s = Fake()
    p = s.plan()
    p["groups"][0]["delete"][0] = bad
    with pytest.raises(ValueError, match="invalid or repeated"):
        run(s, p)
    assert not s.deletes


def test_only_the_exact_preapproved_retired_handoff_may_be_marked(monkeypatch):
    s = Fake()
    p = s.plan()
    handoff_key = s.old.rsplit("/", 1)[0] + "/handoff.json"
    payload = json.dumps({"receipt_key": s.old}).encode()
    p["retired_handoffs"][s.old] = [handoff_key, hashlib.sha256(payload).hexdigest()]
    paginate = s.paginate
    get_object = s.get_object

    def pages(**kwargs):
        result = paginate(**kwargs)
        result[0]["Contents"].append({"Key": handoff_key})
        return result

    def read(**kwargs):
        if kwargs["Key"] == handoff_key:
            return {"Body": io.BytesIO(payload)}
        return get_object(**kwargs)

    monkeypatch.setattr(s, "paginate", pages)
    monkeypatch.setattr(s, "get_object", read)
    assert run(s, p)["objects"] == 1
    p["retired_handoffs"][s.old][1] = "0" * 64
    with pytest.raises(ValueError, match="handoff reference differs"):
        run(s, p)
    assert not s.deletes


def test_apply_records_intent_and_confirmed_absence(tmp_path, monkeypatch):
    s = Fake()
    p = s.plan()
    # Fixture has no surviving handoff, so make the retired-handoff check explicit.
    key = s.old.rsplit("/", 1)[0] + "/handoff.json"
    payload = json.dumps({"receipt_key": s.old}).encode()
    p["retired_handoffs"][s.old] = [key, hashlib.sha256(payload).hexdigest()]
    paginate, get_object = s.paginate, s.get_object

    def pages(**kwargs):
        result = paginate(**kwargs)
        result[0]["Contents"].append({"Key": key})
        return result

    def read(**kwargs):
        if kwargs["Key"] == key:
            return {"Body": io.BytesIO(payload)}
        return get_object(**kwargs)

    s.paginate, s.get_object = pages, read
    with (tmp_path / "journal").open("x") as journal:
        result = run(s, p, apply=True, guard=lambda: None, journal=journal, plan_sha256="a" * 64)
    events = [json.loads(x)["event"] for x in (tmp_path / "journal").read_text().splitlines()]
    assert events == ["intent", "confirmed_absent"]
    assert result["deleted"] == [s.old]
    assert s.keep in s.objects


def _retired_fixture(monkeypatch):
    s = Fake()
    p = s.plan()
    key = s.old.rsplit("/", 1)[0] + "/handoff.json"
    payload = json.dumps({"receipt_key": s.old}).encode()
    p["retired_handoffs"][s.old] = [key, hashlib.sha256(payload).hexdigest()]
    paginate, get_object = s.paginate, s.get_object

    def pages(**kwargs):
        result = paginate(**kwargs)
        result[0]["Contents"].append({"Key": key})
        return result

    def read(**kwargs):
        if kwargs["Key"] == key:
            return {"Body": io.BytesIO(payload)}
        return get_object(**kwargs)

    monkeypatch.setattr(s, "paginate", pages)
    monkeypatch.setattr(s, "get_object", read)
    return s, p


def test_uncertain_delete_is_journalled_and_never_retried(monkeypatch, tmp_path):
    s, p = _retired_fixture(monkeypatch)
    delete = s.delete_object

    def lost_response(**kwargs):
        delete(**kwargs)
        raise TimeoutError("lost response")

    monkeypatch.setattr(s, "delete_object", lost_response)
    path = tmp_path / "journal"
    with path.open("x") as journal, pytest.raises(TimeoutError):
        run(s, p, apply=True, guard=lambda: None, journal=journal, plan_sha256="a" * 64)
    events = [json.loads(x)["event"] for x in path.read_text().splitlines()]
    assert events == ["intent", "outcome_unknown"]
    assert s.deletes == [s.old]


def test_guard_failure_stops_before_deletion(monkeypatch, tmp_path):
    s, p = _retired_fixture(monkeypatch)

    def stop():
        raise RuntimeError("active workflow")

    with (tmp_path / "journal").open("x") as journal, pytest.raises(RuntimeError, match="active"):
        run(s, p, apply=True, guard=stop, journal=journal, plan_sha256="a" * 64)
    assert not s.deletes


def test_journal_failure_stops_before_deletion(monkeypatch, tmp_path):
    s, p = _retired_fixture(monkeypatch)

    def fail(_fd):
        raise OSError("audit unavailable")

    monkeypatch.setattr("baibai_jobs.maintenance.source_input_cleanup.os.fsync", fail)
    with (tmp_path / "journal").open("x") as journal, pytest.raises(OSError, match="audit"):
        run(s, p, apply=True, guard=lambda: None, journal=journal, plan_sha256="a" * 64)
    assert not s.deletes


def test_pointer_change_after_planning_blocks_apply(monkeypatch, tmp_path):
    s, p = _retired_fixture(monkeypatch)

    def change_pointer():
        s.stage = "group-new"

    with (
        (tmp_path / "journal").open("x") as journal,
        pytest.raises(ValueError, match="pointers changed"),
    ):
        run(s, p, apply=True, guard=change_pointer, journal=journal, plan_sha256="a" * 64)
    assert not s.deletes


def test_failed_delete_is_not_retried(monkeypatch, tmp_path):
    s, p = _retired_fixture(monkeypatch)
    calls = []

    def unsupported(**kwargs):
        calls.append(kwargs)
        raise ClientError(
            {"Error": {"Code": "NotImplemented"}, "ResponseMetadata": {"HTTPStatusCode": 501}},
            "DeleteObject",
        )

    monkeypatch.setattr(s, "delete_object", unsupported)
    path = tmp_path / "journal"
    with path.open("x") as journal, pytest.raises(ClientError):
        run(s, p, apply=True, guard=lambda: None, journal=journal, plan_sha256="a" * 64)
    assert len(calls) == 1
    assert "IfMatch" not in calls[0]
    assert s.old in s.objects
    assert [json.loads(x)["event"] for x in path.read_text().splitlines()] == [
        "intent",
        "outcome_unknown",
    ]


def test_github_guard_fails_closed_on_active_or_incomplete(monkeypatch):
    from types import SimpleNamespace

    from baibai_jobs.maintenance.source_input_cleanup import _github_idle

    monkeypatch.setattr(
        "baibai_jobs.maintenance.source_input_cleanup.shutil.which", lambda _name: "/usr/bin/gh"
    )
    monkeypatch.setattr(
        "baibai_jobs.maintenance.source_input_cleanup.subprocess.run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0, stdout='{"total_count": 0, "workflow_runs": []}'
        ),
    )
    _github_idle()
    active = '{"total_count": 1, "workflow_runs": [{"id": 11, "path": ".github/workflows/cloud-market-sources.yml@refs/heads/main"}]}'
    monkeypatch.setattr(
        "baibai_jobs.maintenance.source_input_cleanup.subprocess.run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=active),
    )
    with pytest.raises(RuntimeError, match="active"):
        _github_idle()
    monkeypatch.setattr(
        "baibai_jobs.maintenance.source_input_cleanup.subprocess.run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0, stdout='{"total_count": 1, "workflow_runs": []}'
        ),
    )
    with pytest.raises(RuntimeError, match="incomplete"):
        _github_idle()


def test_botocore_one_attempt_does_not_retry_timeout(monkeypatch):
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ReadTimeoutError

    client = boto3.client(
        "s3",
        endpoint_url="https://example.invalid",
        region_name="auto",
        aws_access_key_id="fixture",
        aws_secret_access_key="fixture",
        config=Config(retries={"mode": "standard", "total_max_attempts": 1}),
    )
    sent = []

    def timeout(request):
        sent.append(request.method)
        raise ReadTimeoutError(endpoint_url="https://example.invalid", error="fixture")

    monkeypatch.setattr(client._endpoint.http_session, "send", timeout)
    with pytest.raises(ReadTimeoutError):
        client.delete_object(Bucket="fixture-bucket", Key="fixture-key")
    assert sent == ["DELETE"]


def test_later_target_mismatch_stops_before_any_delete(monkeypatch, tmp_path):
    s, p = _retired_fixture(monkeypatch)
    other = s.old.replace("group-old2", "group-old3")
    s.objects[other] = s.payload
    item = dict(p["groups"][0]["delete"][0], key=other)
    p["groups"][0]["delete"].append(item)
    original = s.head_object

    def head(**kwargs):
        value = original(**kwargs)
        if kwargs["Key"] == other:
            value["ETag"] = '"changed"'
        return value

    monkeypatch.setattr(s, "head_object", head)
    with (
        (tmp_path / "audit").open("x") as audit,
        pytest.raises(ValueError, match="metadata changed"),
    ):
        run(s, p, apply=True, guard=lambda: None, journal=audit, plan_sha256="a" * 64)
    assert not s.deletes


def test_head_denied_after_delete_stops_and_marks_unknown(monkeypatch, tmp_path):
    s, p = _retired_fixture(monkeypatch)
    original = s.head_object

    def head(**kwargs):
        if kwargs["Key"] == s.old and s.old in s.deletes:
            raise ClientError(
                {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
                "HeadObject",
            )
        return original(**kwargs)

    monkeypatch.setattr(s, "head_object", head)
    path = tmp_path / "audit"
    with path.open("x") as audit, pytest.raises(ClientError):
        run(s, p, apply=True, guard=lambda: None, journal=audit, plan_sha256="a" * 64)
    assert [json.loads(x)["event"] for x in path.read_text().splitlines()] == [
        "intent",
        "outcome_unknown",
    ]
    assert s.deletes == [s.old]


def test_failure_to_sync_confirmation_stops_with_intent(monkeypatch, tmp_path):
    import os

    s, p = _retired_fixture(monkeypatch)
    real_fsync = os.fsync
    count = 0

    def fsync(fd):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("confirmation sync failed")
        real_fsync(fd)

    monkeypatch.setattr("baibai_jobs.maintenance.source_input_cleanup.os.fsync", fsync)
    path = tmp_path / "audit"
    with path.open("x") as audit, pytest.raises(OSError, match="confirmation sync"):
        run(s, p, apply=True, guard=lambda: None, journal=audit, plan_sha256="a" * 64)
    assert json.loads(path.read_text().splitlines()[0])["event"] == "intent"
    assert s.deletes == [s.old]


def test_mixed_retired_and_orphan_plan_stops_before_any_delete(monkeypatch, tmp_path):
    s, p = _retired_fixture(monkeypatch)
    other = s.old.replace("group-old2", "group-old3")
    s.objects[other] = s.payload
    p["groups"][0]["delete"].append(dict(p["groups"][0]["delete"][0], key=other))
    with (
        (tmp_path / "audit").open("x") as audit,
        pytest.raises(ValueError, match="handoff reference"),
    ):
        run(s, p, apply=True, guard=lambda: None, journal=audit, plan_sha256="a" * 64)
    assert not s.deletes


def test_github_paginated_json_detects_batch_on_second_page(monkeypatch):
    from types import SimpleNamespace

    from baibai_jobs.maintenance.source_input_cleanup import _github_idle

    one = {"total_count": 2, "workflow_runs": [{"id": 11, "path": ".github/workflows/ci.yml"}]}
    two = {
        "total_count": 2,
        "workflow_runs": [{"id": 12, "path": ".github/workflows/cloud-daily-batch.yml"}],
    }
    stdout = json.dumps(one) + "\n" + json.dumps(two)
    monkeypatch.setattr(
        "baibai_jobs.maintenance.source_input_cleanup.shutil.which", lambda _: "/usr/bin/gh"
    )
    monkeypatch.setattr(
        "baibai_jobs.maintenance.source_input_cleanup.subprocess.run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=stdout),
    )
    with pytest.raises(RuntimeError, match="batch workflow is active"):
        _github_idle()


@pytest.mark.parametrize(
    "payload",
    [
        '{"total_count":0}',
        '{"total_count":0,"workflow_runs":{}}',
        '{"total_count":0,"workflow_runs":[]}\n{"total_count":1,"workflow_runs":[]}',
        '{"total_count":0,"workflow_runs":[]}\n{"message":"error"}',
    ],
)
def test_github_rejects_malformed_or_inconsistent_pages(monkeypatch, payload):
    from types import SimpleNamespace

    from baibai_jobs.maintenance.source_input_cleanup import _github_idle

    monkeypatch.setattr(
        "baibai_jobs.maintenance.source_input_cleanup.shutil.which", lambda _: "/usr/bin/gh"
    )
    monkeypatch.setattr(
        "baibai_jobs.maintenance.source_input_cleanup.subprocess.run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=payload),
    )
    with pytest.raises(RuntimeError, match="invalid workflow run page"):
        _github_idle()


def test_github_rejects_duplicate_run_ids_across_pages(monkeypatch):
    from types import SimpleNamespace

    from baibai_jobs.maintenance.source_input_cleanup import _github_idle

    page = {"total_count": 2, "workflow_runs": [{"id": 1, "path": ".github/workflows/ci.yml"}]}
    stdout = json.dumps(page) + "\n" + json.dumps(page)
    monkeypatch.setattr(
        "baibai_jobs.maintenance.source_input_cleanup.shutil.which", lambda _: "/usr/bin/gh"
    )
    monkeypatch.setattr(
        "baibai_jobs.maintenance.source_input_cleanup.subprocess.run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=stdout),
    )
    with pytest.raises(RuntimeError, match="duplicate IDs"):
        _github_idle()


@pytest.mark.parametrize("status", [301, 307])
def test_delete_redirect_is_blocked_before_second_wire_send(monkeypatch, status):
    import boto3
    from botocore.awsrequest import AWSResponse
    from botocore.config import Config

    from baibai_jobs.maintenance.source_input_cleanup import _delete_once

    class EmptyBody:
        def stream(self, *_args, **_kwargs):
            yield b""

    client = boto3.client(
        "s3",
        endpoint_url="https://example.test",
        region_name="auto",
        aws_access_key_id="fake",
        aws_secret_access_key="fake",
        config=Config(retries={"total_max_attempts": 1}),
    )
    sent = []

    def redirect(request):
        sent.append(request)
        return AWSResponse(request.url, status, {"x-amz-bucket-region": "us-west-2"}, EmptyBody())

    monkeypatch.setattr(client._endpoint.http_session, "send", redirect)
    with pytest.raises(RuntimeError, match="second DELETE request was blocked"):
        _delete_once(client, "fixture-bucket", "fixture-key")
    assert len(sent) == 1
