from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as temp:
        temp_path = Path(temp.name)
        try:
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    try:
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    """Replace ``path`` with ``payload`` in one visible step.

    Used for the byte strings a retry cannot repair: a reader must see either the
    previous content or the new content, never a truncated write.
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
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
