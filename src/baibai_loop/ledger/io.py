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


def validate_decision_register_jsonl(path: Path) -> list[str]:
    records = read_jsonl(path)
    seen: set[str] = set()
    errors: list[str] = []
    for line_number, record in enumerate(records, start=1):
        record_id = _record_id(record)
        if record_id in seen:
            errors.append(f"line {line_number}: duplicate decision_event_id {record_id}")
        seen.add(record_id)
    return errors


def diff_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> list[str]:
    """Compute a dry-run diff between ``records`` and the JSONL at ``path``.

    Symbols:

    - ``+ id``: 新規 record (upsert で追記される)。
    - ``~ id``: 既存 record の値が変わる (upsert で置換される)。
    - ``! id``: 既存 record だが今回の records には現れない。
      sync は register を再生成するため、実行後はこの行は消える。
    """
    existing = {_record_id(record): record for record in read_jsonl(path)}
    incoming_ids: set[str] = set()
    lines: list[str] = []
    for record in records:
        record_id = _record_id(record)
        incoming_ids.add(record_id)
        current = existing.get(record_id)
        if current is None:
            lines.append(f"+ {record_id}")
        elif current != dict(record):
            lines.append(f"~ {record_id}")
    for orphan_id in sorted(set(existing) - incoming_ids):
        lines.append(f"! {orphan_id}")
    return lines


def _record_id(record: Mapping[str, Any]) -> str:
    value = record.get("decision_event_id")
    if value is None:
        raise KeyError("JSONL record requires decision_event_id")
    return str(value)
