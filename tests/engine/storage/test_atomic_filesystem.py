from pathlib import Path

import pytest

from baibai_engine.storage.filesystem import write_bytes_atomic, write_text_atomic


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
    path = tmp_path / "target" / "result"
    path.parent.mkdir()
    path.write_bytes(b"previous")

    def fail_replace(self, target):
        raise OSError("synthetic replacement failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    writer = write_text_atomic if text else write_bytes_atomic
    with pytest.raises(OSError, match="synthetic replacement failure"):
        writer(path, "new" if text else b"new")
    assert path.read_bytes() == b"previous"
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("failure", [False, True])
def test_directory_fsync_closes_its_descriptor_even_on_failure(tmp_path, monkeypatch, failure):
    import os

    from baibai_engine.storage.filesystem import fsync_directory

    descriptors = []
    real_fsync = os.fsync

    def sync(descriptor):
        descriptors.append(descriptor)
        if failure:
            raise OSError("synthetic directory sync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", sync)
    if failure:
        with pytest.raises(OSError, match="synthetic directory sync failure"):
            fsync_directory(tmp_path)
    else:
        fsync_directory(tmp_path)
    assert len(descriptors) == 1
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(descriptors[0])


def test_new_text_export_creates_parents_but_never_replaces(tmp_path):
    from baibai_engine.storage.filesystem import write_text_new

    target = tmp_path / "nested" / "export.txt"
    write_text_new(target, "日本語\n")
    assert target.read_text(encoding="utf-8") == "日本語\n"
    with pytest.raises(FileExistsError):
        write_text_new(target, "replacement")
    assert target.read_text(encoding="utf-8") == "日本語\n"


@pytest.mark.parametrize("existing_target", [False, True])
def test_new_text_export_does_not_follow_symlinks(tmp_path, existing_target):
    from baibai_engine.storage.filesystem import write_text_new

    target, link = tmp_path / "target", tmp_path / "link"
    if existing_target:
        target.write_text("retained", encoding="utf-8")
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        write_text_new(link, "replacement")
    assert link.is_symlink()
    assert target.exists() is existing_target
    if existing_target:
        assert target.read_text(encoding="utf-8") == "retained"
