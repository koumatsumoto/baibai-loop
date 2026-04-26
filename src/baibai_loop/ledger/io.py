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
    existing = {str(record["ledger_id"]): record for record in read_jsonl(path)}
    lines: list[str] = []
    for record in records:
        ledger_id = str(record["ledger_id"])
        current = existing.get(ledger_id)
        if current is None:
            lines.append(f"+ {ledger_id}")
        elif current != dict(record):
            lines.append(f"~ {ledger_id}")
    return lines
