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

from .io import diff_jsonl, write_jsonl
from .records import DecisionRegisterRecord, Tracking
from .tracking import resolve_tracking_prices

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_TrackingMode = Literal["post_approval", "re_examination", "none"]


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
        macro_context_fit = _mapping_or_none(front.get("macro_context_fit"))
        outcome = str(research_decision.get("outcome") or "deferred")
        candidate_ref = _mapping_or_none(front.get("candidate_ref"))
        candidate = _candidate_from_ref(candidates_index, candidate_ref)
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
            research_decision=dict(research_decision),
            candidate_ref=dict(candidate_ref) if candidate_ref else None,
            research_ref=str(path.relative_to(root)),
            trade_ref=None,
            decision_event_at=decision_event_at.isoformat(),
            playbook_id=playbook_id,
            playbook_ref=dict(front["playbook_ref"])
            if isinstance(front.get("playbook_ref"), Mapping)
            else None,
            macro_context_ref=str(front.get("macro_context_ref") or ""),
            macro_context_fit=dict(macro_context_fit) if macro_context_fit else None,
            macro_context_decision_effect=str(macro_context_fit.get("decision_effect") or "")
            if macro_context_fit
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
            tracking=Tracking(mode="post_approval"),
        )
        records.append(record.to_json())

    by_month = _group_by_month(records)
    diff_lines: list[str] = []
    for month, rows in by_month.items():
        path = register_root / f"{month}.jsonl"
        if dry_run:
            diff_lines.extend(diff_jsonl(path, rows))
        else:
            write_jsonl(path, rows)
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


def _candidate_from_ref(
    candidates_index: dict[str, dict[str, Mapping[str, Any]]],
    candidate_ref: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if candidate_ref is None:
        return {}
    candidates_ref = candidate_ref.get("candidates_ref")
    ticker = candidate_ref.get("ticker")
    candidate_id = candidate_ref.get("candidate_id")
    screen_run_id = candidate_ref.get("screen_run_id")
    if not isinstance(candidates_ref, str) or not isinstance(ticker, str):
        return {}
    candidate = candidates_index.get(candidates_ref, {}).get(ticker, {})
    if not candidate:
        return {}
    if isinstance(candidate_id, str) and candidate.get("candidate_id") != candidate_id:
        return {}
    if isinstance(screen_run_id, str) and candidate.get("screen_run_id") != screen_run_id:
        return {}
    return candidate


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
