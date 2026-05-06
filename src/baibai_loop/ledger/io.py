from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from baibai_loop.screening.filesystem import write_text_atomic


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


def upsert_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
    existing_rows = read_jsonl(path)
    existing = {_record_id(record): record for record in existing_rows}
    additions: list[dict[str, Any]] = []
    for record in records:
        record_id = _record_id(record)
        current = existing.get(record_id)
        if current is None:
            addition = dict(record)
            additions.append(addition)
            existing[record_id] = addition
            continue
        merged = _merge_record(current, record)
        if merged != current:
            raise ValueError(
                "decision register is append-only; write a correction event instead of "
                f"rewriting {record_id}"
            )
    if not additions:
        return 0, 0
    content = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in (*existing_rows, *additions)
    )
    write_text_atomic(path, content)
    return len(additions), 0


def validate_append_only_jsonl(path: Path) -> list[str]:
    records = read_jsonl(path)
    seen: set[str] = set()
    corrections: dict[str, str] = {}
    errors: list[str] = []
    for line_number, record in enumerate(records, start=1):
        record_id = _record_id(record)
        if record_id in seen:
            errors.append(f"line {line_number}: duplicate decision_event_id {record_id}")
        seen.add(record_id)
        if record.get("event_kind") != "correction":
            continue
        target = record.get("corrects_event_id")
        if not isinstance(target, str) or not target:
            errors.append(f"line {line_number}: correction requires corrects_event_id")
            continue
        if target not in seen:
            errors.append(f"line {line_number}: correction target must appear earlier: {target}")
        corrections[record_id] = target
    for source in corrections:
        visited = {source}
        current = corrections[source]
        while current in corrections:
            if current in visited:
                errors.append(f"correction cycle involving {source}")
                break
            visited.add(current)
            current = corrections[current]
    return errors


def diff_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> list[str]:
    """Compute a dry-run diff between ``records`` and the JSONL at ``path``.

    Symbols:

    - ``+ id``: 新規 record (upsert で追記される)。
    - ``~ id``: 既存 record の値が変わる (upsert で置換される)。
    - ``! id``: 既存 record だが今回の records には現れない orphan。
      ledger は audit log なので upsert は削除しない。orphan は
      research packet が消えた等の状況で発生し、retro 確認用の通知。
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
        elif current != _merge_record(current, record):
            lines.append(f"~ {record_id}")
    for orphan_id in sorted(set(existing) - incoming_ids):
        lines.append(f"! {orphan_id}")
    return lines


def append_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> int:
    existing = read_jsonl(path)
    additions = [dict(record) for record in records]
    if not additions:
        return 0
    content = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in (*existing, *additions)
    )
    write_text_atomic(path, content)
    return len(additions)


def _record_id(record: Mapping[str, Any]) -> str:
    value = record.get("decision_event_id")
    if value is None:
        raise KeyError("JSONL record requires decision_event_id")
    return str(value)


def _merge_record(
    current: Mapping[str, Any] | None,
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    if current is None:
        return dict(incoming)
    merged = dict(current)
    merged.update(dict(incoming))
    return merged
