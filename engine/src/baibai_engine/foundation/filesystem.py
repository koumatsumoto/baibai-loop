from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_text_atomic(path: Path, content: str) -> None:
    """Write UTF-8 text through the shared atomic byte replacement."""
    write_bytes_atomic(path, content.encode("utf-8"))


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    """Replace ``path`` with ``payload`` in one visible step.

    A reader sees either the previous content or the complete new content,
    never a partially written payload.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("wb", delete=False, dir=path.parent) as temp:
        temp_path = Path(temp.name)
        try:
            temp.write(payload)
            temp.flush()
            os.fsync(temp.fileno())
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    try:
        temp_path.replace(path)
        _fsync_directory(path.parent)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
