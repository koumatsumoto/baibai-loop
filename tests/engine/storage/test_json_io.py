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
    expected = '{\n  "count": 0,\n  "missing": null,\n  "status": "完了"\n}\n'
    assert path.read_bytes() == expected.encode("utf-8")
    assert list(path.parent.iterdir()) == [path]


def test_failed_serialization_keeps_prior_file_and_creates_no_temporary(tmp_path):
    directory = tmp_path / "json-output"
    directory.mkdir()
    path = directory / "result.json"
    path.write_text("old")

    with pytest.raises(TypeError, match="not JSON serializable"):
        json_io.write_json_atomic(path, {"status": "new", "unsupported": object()})
    assert path.read_text() == "old"
    assert list(directory.iterdir()) == [path]


def test_notification_module_does_not_own_storage_writer():
    from baibai_jobs.observability import discord

    assert not hasattr(discord, "write_json_atomic")


def test_failed_atomic_replace_keeps_the_old_json_and_removes_temporary(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "json-output" / "result.json"
    path.parent.mkdir()
    path.write_bytes(b"previous")

    def fail_replace(self, target):
        raise OSError("synthetic rename failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic rename failure"):
        json_io.write_json_atomic(path, {"status": "new"})
    assert path.read_bytes() == b"previous"
    assert list(path.parent.iterdir()) == [path]
