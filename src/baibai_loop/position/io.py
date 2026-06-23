from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from baibai_loop.foundation.filesystem import write_text_atomic


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_number}: JSONL line must be an object")
        records.append(payload)
    return records


def write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> int:
    rows = [dict(record) for record in records]
    content = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in rows
    )
    write_text_atomic(path, content)
    return len(rows)
