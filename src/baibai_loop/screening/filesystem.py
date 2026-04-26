from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as temp:
        temp.write(content)
        temp.flush()
        os.fsync(temp.fileno())
        temp_path = Path(temp.name)

    temp_path.replace(path)
