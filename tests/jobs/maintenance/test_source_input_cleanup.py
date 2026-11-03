import hashlib
import io
import json
from copy import deepcopy

import pytest
from botocore.exceptions import ClientError

from baibai_jobs.maintenance.source_input_cleanup import run


class Fake:
    def __init__(self):
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
        assert kwargs["IfMatch"] == '"object"'
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


def test_dry_run_has_no_deletes_and_apply_is_disabled():
    s = Fake()
    plan = s.plan()
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


def test_missing_or_corrupt_survivor_fails_before_any_delete():
    s = Fake()
    p = s.plan()
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


def test_apply_records_intent_and_confirmed_absence(tmp_path):
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


def test_unsupported_conditional_delete_does_not_fall_back(monkeypatch, tmp_path):
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
    assert calls[0]["IfMatch"] == '"object"'
    assert s.old in s.objects
    assert [json.loads(x)["event"] for x in path.read_text().splitlines()] == [
        "intent",
        "outcome_unknown",
    ]
