from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from .io import read_jsonl


@dataclass(frozen=True, slots=True)
class Tracking:
    mode: Literal["post_approval", "re_examination", "none"]
    plus_15bd: float | None = None
    plus_30bd: float | None = None
    plus_15bd_source: dict[str, object] | None = None
    plus_30bd_source: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class DecisionRegisterRecord:
    decision_event_id: str
    event_kind: Literal["decision", "correction"]
    decision_scope: Literal["research_memo", "trade_execution"]
    ticker: str
    name: str
    execution_state: str
    thesis_decision: dict[str, object] | None = None
    order_intent: dict[str, object] | None = None
    candidate_ref: dict[str, object] | None = None
    thesis_ref: str | None = None
    position_ref: str | None = None
    decision_event_at: str | None = None
    playbook_id: str | None = None
    playbook_ref: dict[str, object] | None = None
    macro_context_ref: str | None = None
    macro_context_fit: dict[str, object] | None = None
    macro_context_decision_effect: str | None = None
    baseline_price: float | None = None
    market_cap_oku: float | None = None
    avg_turnover_oku: float | None = None
    tracking: Tracking | None = None

    def to_json(self) -> dict[str, object]:
        payload = asdict(self)
        tracking = payload.get("tracking")
        if isinstance(tracking, dict):
            payload["tracking"] = {
                key: value
                for key, value in tracking.items()
                if value is not None or key in {"plus_15bd", "plus_30bd"}
            }
        return {key: value for key, value in payload.items() if value is not None}


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
