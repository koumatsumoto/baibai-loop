import hashlib
import io
import json
from copy import deepcopy

import pytest

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
            "pointers": {self.pointer: ['"pointer"', self.stage]},
            "groups": [{"keep": item(self.keep), "delete": [item(self.old)]}],
        }


def test_dry_run_has_no_deletes_and_apply_is_disabled():
    s = Fake()
    plan = s.plan()
    assert run(s, plan)["objects"] == 1
    assert not s.deletes
    with pytest.raises(RuntimeError, match="apply disabled"):
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
    with pytest.raises(ValueError, match="surviving handoff"):
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
