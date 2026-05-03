from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from baibai_loop.playbooks import playbook_short
from baibai_loop.screening.providers.jquants import JQuantsDailyBar
from baibai_loop.screening.render import JST

from .io import append_jsonl, diff_jsonl, read_jsonl, upsert_jsonl
from .records import PaperLedgerRecord, SkippedLedgerRecord, Tracking
from .tracking import resolve_price_on_or_before, resolve_tracking_prices

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_LATEST_PRICE_RE = re.compile(r"最新 adj close \([^)]*\) \| ([0-9,]+(?:\.[0-9]+)?) 円")
# 既存 record と incoming record で差分があったときに updates ledger に event を残す
# 対象。tracking 系 (price 解決) と decision 系 (state 遷移) を残し、retro 集計で
# 「いつ pending → accepted になったか」「どの時点で adjustment 入ったか」を ledger
# 単独で追えるようにする。docs/components/ledger.md と同期。
_TRACKED_UPDATE_FIELDS = (
    "baseline_price",
    "adjustment_applied",
    "tracking",
    "decision",
    "macro_gate",
    "adv_participation_pct",
)


@dataclass(frozen=True, slots=True)
class SyncResult:
    paper_count: int
    skipped_count: int
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
    observed = (observed_at or datetime.now(UTC)).isoformat()
    research_root = root / "records/04-research"
    ledger_root = root / "records/_ledger"
    candidates_index = _load_candidates(root)
    paper_records: list[dict[str, Any]] = []
    skipped_records: list[dict[str, Any]] = []
    warnings: list[str] = []
    research_keys: set[tuple[str, str]] = set()
    for path in sorted(research_root.rglob("*.md")):
        parsed = _parse_research(path)
        if parsed is None:
            warnings.append(f"skip malformed research file: {path}")
            continue
        front, body = parsed
        decision = str(front.get("decision", "pending"))
        ticker = str(front["ticker"])
        playbook = str(front["playbook"])
        research_keys.add((ticker, playbook))
        candidates_ref = str(front["candidates_ref"])
        candidate = candidates_index.get(candidates_ref, {}).get(ticker, {})
        decision_date = _decision_date(path, front)
        baseline_price, adjustment_applied, price_warning = _baseline_price(
            ticker, decision_date, body, bars
        )
        if price_warning:
            warnings.append(f"{path}: {price_warning}")
        avg_turnover = _float_or_none(candidate.get("avg_turnover_oku")) or _extract_avg_turnover(
            body
        )
        position_size = _float_or_none(front.get("position_size_oku"))
        adv_participation = (
            round(position_size / avg_turnover * 100, 2)
            if position_size is not None and avg_turnover and avg_turnover > 0
            else None
        )
        plus_15bd, plus_30bd = (
            resolve_tracking_prices(ticker, decision_date, calendar, bars)
            if calendar and bars
            else (None, None)
        )
        name = str(front["name"])
        research_ref = str(path.relative_to(root))
        asof_date = _asof_date(candidates_ref)
        market_cap = _float_or_none(candidate.get("market_cap_oku")) or _extract_market_cap(body)
        threshold_hit_count = (
            len(candidate.get("threshold_hit", []))
            if isinstance(candidate.get("threshold_hit"), list)
            else 0
        )
        macro_gate = str(front.get("macro_gate", ""))
        tracking = Tracking(plus_15bd=plus_15bd, plus_30bd=plus_30bd)
        short = playbook_short(playbook)
        if decision in {"accepted", "pending"}:
            record = PaperLedgerRecord(
                ledger_id=f"paper-{decision_date:%Y%m%d}-{ticker}-{short}",
                decision=cast(Literal["accepted", "pending"], decision),
                ticker=ticker,
                name=name,
                playbook=playbook,
                candidates_ref=candidates_ref,
                research_ref=research_ref,
                asof_date=asof_date,
                decision_date=decision_date.isoformat(),
                baseline_price=baseline_price,
                market_cap_oku=market_cap,
                avg_turnover_oku=avg_turnover,
                threshold_hit_count=threshold_hit_count,
                macro_gate=macro_gate,
                adv_participation_pct=adv_participation,
                adjustment_applied=adjustment_applied,
                tracking=tracking,
            )
            paper_records.append(record.to_json())
        elif decision == "skipped":
            skipped_record = SkippedLedgerRecord(
                ledger_id=f"skipped-{decision_date:%Y%m%d}-{ticker}-{short}",
                decision="skipped",
                ticker=ticker,
                name=name,
                playbook=playbook,
                candidates_ref=candidates_ref,
                research_ref=research_ref,
                asof_date=asof_date,
                decision_date=decision_date.isoformat(),
                baseline_price=baseline_price,
                market_cap_oku=market_cap,
                avg_turnover_oku=avg_turnover,
                threshold_hit_count=threshold_hit_count,
                macro_gate=macro_gate,
                adv_participation_pct=adv_participation,
                adjustment_applied=adjustment_applied,
                tracking=tracking,
            )
            skipped_records.append(skipped_record.to_json())
    select_dir = root / "select"
    if select_dir.exists():
        skipped_records.extend(
            _load_select_skipped(root, candidates_index, research_keys, warnings)
        )
    else:
        warnings.append("select/ does not exist; skipped candidate-only ledger population")
    paper_by_month = _group_by_month(paper_records)
    skipped_by_month = _group_by_month(skipped_records)
    diff_lines: list[str] = []
    for month, records in paper_by_month.items():
        path = ledger_root / "paper" / f"{month}.jsonl"
        if dry_run:
            diff_lines.extend(diff_jsonl(path, records))
        else:
            _write_update_events(root, "paper", month, records, observed)
            upsert_jsonl(path, records)
    for month, records in skipped_by_month.items():
        path = ledger_root / "skipped" / f"{month}.jsonl"
        if dry_run:
            diff_lines.extend(diff_jsonl(path, records))
        else:
            _write_update_events(root, "skipped", month, records, observed)
            upsert_jsonl(path, records)
    if dry_run:
        diff_lines.extend(_removed_month_diffs(root, "paper", paper_by_month))
        diff_lines.extend(_removed_month_diffs(root, "skipped", skipped_by_month))
    return SyncResult(
        paper_count=len(paper_records),
        skipped_count=len(skipped_records),
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
    for path in sorted((root / "records/03-candidates").rglob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            continue
        by_ticker: dict[str, Mapping[str, Any]] = {}
        tickers = document.get("tickers", [])
        if isinstance(tickers, list):
            for item in tickers:
                if isinstance(item, Mapping) and isinstance(item.get("ticker"), str):
                    by_ticker[str(item["ticker"])] = item
        loaded[str(path.relative_to(root))] = by_ticker
    return loaded


def _load_select_skipped(
    root: Path,
    candidates_index: Mapping[str, Mapping[str, Mapping[str, Any]]],
    research_keys: set[tuple[str, str]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((root / "select").glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, Mapping):
            warnings.append(f"skip malformed select file: {path}")
            continue
        decision_date = _select_decision_date(path, document)
        candidates_ref = str(document.get("candidates_ref") or "")
        candidates = _select_candidates(document)
        for candidate in candidates:
            ticker_raw = candidate.get("ticker")
            if not isinstance(ticker_raw, str):
                continue
            ticker = ticker_raw
            playbook = str(candidate.get("playbook") or document.get("playbook") or "default")
            if (ticker, playbook) in research_keys:
                continue
            candidate_info = candidates_index.get(candidates_ref, {}).get(ticker, {})
            short = playbook_short(playbook) if playbook != "default" else "default"
            record = SkippedLedgerRecord(
                ledger_id=f"skipped-{decision_date:%Y%m%d}-{ticker}-{short}",
                ticker=ticker,
                name=str(candidate.get("name") or candidate_info.get("name") or ""),
                decision="skipped",
                playbook=playbook,
                candidates_ref=candidates_ref,
                research_ref=None,
                asof_date=(
                    _asof_date(candidates_ref) if candidates_ref else decision_date.isoformat()
                ),
                decision_date=decision_date.isoformat(),
                baseline_price=None,
                market_cap_oku=_float_or_none(candidate_info.get("market_cap_oku")),
                avg_turnover_oku=_float_or_none(candidate_info.get("avg_turnover_oku")),
                threshold_hit_count=len(candidate_info.get("threshold_hit", []))
                if isinstance(candidate_info.get("threshold_hit"), list)
                else 0,
                macro_gate=None,
                adv_participation_pct=None,
                adjustment_applied=False,
                tracking=Tracking(plus_15bd=None, plus_30bd=None),
            )
            records.append(record.to_json())
    return records


def _select_candidates(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("candidates", "tickers", "selected"):
        value = document.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _select_decision_date(path: Path, document: Mapping[str, Any]) -> date:
    for key in ("decision_date", "asof_date", "run_date"):
        value = document.get(key)
        if isinstance(value, str):
            return date.fromisoformat(value[:10])
    return date.fromisoformat(path.stem[:10])


def _decision_date(path: Path, front: Mapping[str, Any]) -> date:
    published = front.get("published_at")
    if isinstance(published, str):
        normalized = published.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        # naive datetime は astimezone でランナーの local time として解釈されてしまう
        # ため、明示的に JST を付与する。research front matter は JST 前提。
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=JST)
        return parsed.astimezone(JST).date()
    return date.fromisoformat(path.name[:10])


def _asof_date(candidates_ref: str) -> str:
    return Path(candidates_ref).stem[:10]


def _baseline_price(
    ticker: str,
    decision_date: date,
    body: str,
    bars: tuple[JQuantsDailyBar, ...],
) -> tuple[float | None, bool, str | None]:
    if bars:
        price, adjusted = resolve_price_on_or_before(ticker, decision_date, bars)
        if price is not None:
            return price, adjusted, None
    match = _LATEST_PRICE_RE.search(body)
    if match:
        return float(match.group(1).replace(",", "")), False, None
    return None, False, "baseline_price could not be resolved from J-Quants bars or body fallback"


def _extract_market_cap(body: str) -> float | None:
    match = re.search(r"時価総額: ([0-9,]+(?:\.[0-9]+)?) 億円", body)
    return float(match.group(1).replace(",", "")) if match else None


def _extract_avg_turnover(body: str) -> float | None:
    match = re.search(r"avg 約 ([0-9,]+(?:\.[0-9]+)?) 億円/日", body)
    return float(match.group(1).replace(",", "")) if match else None


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _group_by_month(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        month = str(record["decision_date"])[:7]
        grouped.setdefault(month, []).append(record)
    return grouped


def _write_update_events(
    root: Path,
    ledger_type: str,
    month: str,
    records: list[dict[str, Any]],
    observed_at: str,
) -> None:
    existing = {
        str(record["ledger_id"]): record
        for record in read_jsonl(_ledger_path(root, ledger_type, month))
    }
    events: list[dict[str, Any]] = []
    for record in records:
        ledger_id = str(record["ledger_id"])
        previous = existing.get(ledger_id)
        if previous is None:
            continue
        for field in _TRACKED_UPDATE_FIELDS:
            old = previous.get(field)
            new = record.get(field)
            if old != new:
                events.append(
                    {
                        "ledger_id": ledger_id,
                        "field": field,
                        "old": old,
                        "new": new,
                        "observed_at": observed_at,
                    }
                )
    append_jsonl(root / "records/_ledger" / "updates" / f"{month}.jsonl", events)


def _ledger_path(root: Path, ledger_type: str, month: str) -> Path:
    return root / "records/_ledger" / ledger_type / f"{month}.jsonl"


def _removed_month_diffs(
    root: Path,
    ledger_type: str,
    grouped: Mapping[str, list[dict[str, Any]]],
) -> list[str]:
    lines: list[str] = []
    for path in sorted((root / "records/_ledger" / ledger_type).glob("*.jsonl")):
        if path.stem not in grouped:
            lines.extend(diff_jsonl(path, []))
    return lines
