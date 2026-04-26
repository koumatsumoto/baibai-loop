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
    existing = {str(record["ledger_id"]): record for record in read_jsonl(path)}
    before = dict(existing)
    for record in records:
        existing[str(record["ledger_id"])] = dict(record)
    ordered = sorted(
        existing.values(),
        key=lambda item: (str(item.get("decision_date", "")), str(item.get("ticker", ""))),
    )
    content = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for record in ordered
    )
    write_text_atomic(path, content)
    added = len(set(existing) - set(before))
    changed = sum(1 for key, record in existing.items() if before.get(key) != record)
    return added, changed


def diff_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> list[str]:
    """Compute a dry-run diff between ``records`` and the JSONL at ``path``.

    Symbols:

    - ``+ id``: 新規 record (upsert で追記される)。
    - ``~ id``: 既存 record の値が変わる (upsert で置換される)。
    - ``! id``: 既存 record だが今回の records には現れない orphan。
      ledger は audit log なので upsert は削除しない。orphan は
      research packet が消えた等の状況で発生し、retro 確認用の通知。
    """
    existing = {str(record["ledger_id"]): record for record in read_jsonl(path)}
    incoming_ids: set[str] = set()
    lines: list[str] = []
    for record in records:
        ledger_id = str(record["ledger_id"])
        incoming_ids.add(ledger_id)
        current = existing.get(ledger_id)
        if current is None:
            lines.append(f"+ {ledger_id}")
        elif current != dict(record):
            lines.append(f"~ {ledger_id}")
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
