from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from baibai_loop.screening.providers.jquants import JQuantsDailyBar
from baibai_loop.screening.render import JST

from .io import diff_jsonl, upsert_jsonl
from .records import DecisionRegisterRecord, Tracking
from .tracking import resolve_tracking_prices

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_TrackingMode = Literal["post_approval", "re_examination", "missed_opportunity", "none"]


@dataclass(frozen=True, slots=True)
class SyncResult:
    decision_count: int
    warnings: tuple[str, ...]
    diff_lines: tuple[str, ...]


def sync_ledger(
    root: Path,
    *,
    dry_run: bool = False,
    calendar: tuple[date, ...] = (),
    bars: tuple[JQuantsDailyBar, ...] = (),
    observed_at: datetime | None = None,
) -> SyncResult:
    _ = (observed_at or datetime.now(UTC)).isoformat()
    research_root = root / "records/05-research"
    trade_root = root / "records/06-trades"
    register_root = root / "records/_ledger/research-decisions"
    candidates_index = _load_candidates(root)
    records: list[dict[str, Any]] = []
    warnings: list[str] = []

    for path in sorted(research_root.rglob("*.md")):
        parsed = _parse_research(path)
        if parsed is None:
            warnings.append(f"skip malformed investment memo: {path}")
            continue
        front, _body = parsed
        ticker = str(front["ticker"])
        playbook_id = str(front["playbook_id"])
        decision_event_at = _decision_datetime(path, front)
        research_decision = _mapping_or_none(front.get("research_decision")) or {}
        outcome = str(research_decision.get("outcome") or "deferred")
        candidate_decision = _candidate_decision_from_research(outcome)
        candidate_ref = _mapping_or_none(front.get("candidate_ref"))
        candidates_ref = str(front.get("candidates_ref") or "")
        candidate = candidates_index.get(candidates_ref, {}).get(ticker, {})
        plus_15bd, plus_30bd = (
            resolve_tracking_prices(ticker, decision_event_at.date(), calendar, bars)
            if calendar and bars
            else (None, None)
        )
        tracking = _tracking_from_front(front, plus_15bd, plus_30bd, outcome)
        record = DecisionRegisterRecord(
            decision_event_id=f"decision-{decision_event_at:%Y%m%d}-{ticker}-research",
            event_kind="decision",
            decision_scope="research_memo",
            ticker=ticker,
            name=str(front.get("name") or ""),
            trade_execution_state="none",
            candidate_decision=candidate_decision,
            research_decision=dict(research_decision),
            candidate_ref=dict(candidate_ref) if candidate_ref else None,
            research_ref=str(path.relative_to(root)),
            trade_ref=None,
            decision_event_at=decision_event_at.isoformat(),
            playbook_id=playbook_id,
            playbook_snapshot=dict(front["playbook_snapshot"])
            if isinstance(front.get("playbook_snapshot"), Mapping)
            else None,
            policy_snapshot=dict(front["policy_snapshot"])
            if isinstance(front.get("policy_snapshot"), Mapping)
            else None,
            baseline_price=_float_or_none(candidate.get("last_price"))
            or _float_or_none(candidate.get("baseline_price")),
            market_cap_oku=_float_or_none(candidate.get("market_cap_oku"))
            or _float_or_none(front.get("market_cap_oku")),
            avg_turnover_oku=_float_or_none(candidate.get("avg_turnover_oku"))
            or _float_or_none(front.get("avg_turnover_oku")),
            independent_evidence_count=_int_or_none(front.get("independent_evidence_count")),
            conviction_tier=str(front.get("conviction_tier") or ""),
            tracking=tracking,
        )
        records.append(record.to_json())

    for path in sorted(trade_root.rglob("*.md")):
        parsed = _parse_research(path)
        if parsed is None:
            warnings.append(f"skip malformed trade record: {path}")
            continue
        front, _body = parsed
        order_intent = _mapping_or_none(front.get("order_intent"))
        if order_intent is None:
            warnings.append(f"skip trade without order_intent: {path}")
            continue
        ticker = str(front["ticker"])
        decision_event_at = _trade_decision_datetime(path, front)
        decision_event_id = str(
            order_intent.get("decision_event_id")
            or f"decision-{decision_event_at:%Y%m%d}-{ticker}-trade"
        )
        record = DecisionRegisterRecord(
            decision_event_id=decision_event_id,
            event_kind="decision",
            decision_scope="trade_execution",
            ticker=ticker,
            name=str(front.get("name") or ""),
            trade_execution_state=str(front.get("trade_execution_state") or "none"),
            order_intent=dict(order_intent),
            research_ref=str(front.get("research_ref") or ""),
            trade_ref=str(path.relative_to(root)),
            decision_event_at=decision_event_at.isoformat(),
            policy_snapshot=dict(front["policy_snapshot"])
            if isinstance(front.get("policy_snapshot"), Mapping)
            else None,
        )
        records.append(record.to_json())

    by_month = _group_by_month(records)
    diff_lines: list[str] = []
    for month, rows in by_month.items():
        path = register_root / f"{month}.jsonl"
        if dry_run:
            diff_lines.extend(diff_jsonl(path, rows))
        else:
            upsert_jsonl(path, rows)
    return SyncResult(
        decision_count=len(records),
        warnings=tuple(warnings),
        diff_lines=tuple(diff_lines),
    )


def _parse_research(path: Path) -> tuple[dict[str, Any], str] | None:
    match = _FRONT_MATTER_RE.match(path.read_text(encoding="utf-8"))
    if not match:
        return None
    front = yaml.safe_load(match.group(1))
    if not isinstance(front, dict):
        return None
    return front, match.group(2)


def _load_candidates(root: Path) -> dict[str, dict[str, Mapping[str, Any]]]:
    loaded: dict[str, dict[str, Mapping[str, Any]]] = {}
    for path in sorted((root / "records/04-candidates").rglob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            continue
        by_ticker: dict[str, Mapping[str, Any]] = {}
        candidates = document.get("candidates", [])
        if isinstance(candidates, list):
            for item in candidates:
                if isinstance(item, Mapping) and isinstance(item.get("ticker"), str):
                    by_ticker[str(item["ticker"])] = item
        loaded[str(path.relative_to(root))] = by_ticker
    return loaded


def _decision_datetime(path: Path, front: Mapping[str, Any]) -> datetime:
    for key in ("recorded_at", "published_at"):
        value = front.get(key)
        if isinstance(value, str):
            return _parse_jst_datetime(value)
    return datetime.fromisoformat(f"{path.name[:10]}T00:00:00+09:00")


def _trade_decision_datetime(path: Path, front: Mapping[str, Any]) -> datetime:
    orders = front.get("orders")
    if isinstance(orders, list):
        for order in orders:
            if not isinstance(order, Mapping):
                continue
            events = order.get("events")
            if not isinstance(events, list):
                continue
            for event in events:
                if not isinstance(event, Mapping):
                    continue
                value = event.get("at")
                if isinstance(value, str):
                    return _parse_jst_datetime(value)
    return _decision_datetime(path, front)


def _parse_jst_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


def _candidate_decision_from_research(outcome: str) -> str:
    match outcome:
        case "approved":
            return "selected"
        case "rejected":
            return "rejected"
        case _:
            return "deferred"


def _tracking_from_front(
    front: Mapping[str, Any],
    plus_15bd: float | None,
    plus_30bd: float | None,
    outcome: str,
) -> Tracking:
    tracking = _mapping_or_none(front.get("tracking")) or {}
    raw_mode = tracking.get("mode")
    mode: _TrackingMode
    if raw_mode == "post_approval":
        mode = "post_approval"
    elif raw_mode == "re_examination":
        mode = "re_examination"
    elif raw_mode == "missed_opportunity":
        mode = "missed_opportunity"
    elif raw_mode == "none":
        mode = "none"
    else:
        mode = "post_approval" if outcome == "approved" else "re_examination"
    return Tracking(
        mode=mode,
        plus_15bd=plus_15bd if plus_15bd is not None else _float_or_none(tracking.get("plus_15bd")),
        plus_30bd=plus_30bd if plus_30bd is not None else _float_or_none(tracking.get("plus_30bd")),
    )


def _group_by_month(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        event_at = str(record.get("decision_event_at") or "")
        month = event_at[:7] if event_at else "unknown"
        grouped.setdefault(month, []).append(record)
    return grouped


def _mapping_or_none(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    return None
