"""Jobsの技術的JSON保存は通知adapterへ依存しない。"""

import json

import pytest

import baibai_engine.storage.json as json_io


def test_canonical_json_keeps_stable_utf8_bytes():
    assert json_io.canonical_json({"b": 2, "a": "日本"}) == '{"a":"日本","b":2}'.encode()


def test_atomic_json_replaces_with_complete_payload(tmp_path):
    path = tmp_path / "nested" / "result.json"
    json_io.write_json_atomic(path, {"status": "旧"})
    json_io.write_json_atomic(path, {"count": 0, "missing": None, "status": "完了"})
    assert json.loads(path.read_text()) == {"count": 0, "missing": None, "status": "完了"}
    assert path.read_text().endswith("\n")
    assert list(path.parent.iterdir()) == [path]


def test_failed_serialization_keeps_prior_file_and_removes_temporary(tmp_path, monkeypatch):
    directory = tmp_path / "json-output"
    directory.mkdir()
    path = directory / "result.json"
    path.write_text("old")

    def fail(payload, stream, **kwargs):
        stream.write("partial")
        raise ValueError("serialization failed")

    monkeypatch.setattr(json_io.json, "dump", fail)
    with pytest.raises(ValueError, match="serialization failed"):
        json_io.write_json_atomic(path, {"status": "new"})
    assert path.read_text() == "old"
    assert list(directory.iterdir()) == [path]


def test_notification_module_does_not_own_storage_writer():
    from baibai_jobs.observability import discord

    assert not hasattr(discord, "write_json_atomic")
