from pathlib import Path

import pytest

from baibai_engine.foundation.filesystem import write_bytes_atomic, write_text_atomic


@pytest.mark.parametrize("text", [False, True])
def test_atomic_writer_creates_parent_and_replaces_complete_content(tmp_path, text):
    path = tmp_path / "nested" / "result"
    writer = write_text_atomic if text else write_bytes_atomic
    payload = "日本語\n次の行\n" if text else b"\x00\xff\n"
    writer(path, payload)
    assert path.read_bytes() == (payload.encode("utf-8") if text else payload)
    writer(path, "replacement" if text else b"replacement")
    assert path.read_bytes() == b"replacement"
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("text", [False, True])
def test_atomic_writer_preserves_previous_content_when_replace_fails(tmp_path, monkeypatch, text):
    path = tmp_path / "result"
    path.write_bytes(b"previous")

    def fail_replace(self, target):
        raise OSError("synthetic replacement failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    writer = write_text_atomic if text else write_bytes_atomic
    with pytest.raises(OSError, match="synthetic replacement failure"):
        writer(path, "new" if text else b"new")
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [path]
