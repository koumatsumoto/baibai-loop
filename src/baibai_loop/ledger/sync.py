from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from baibai_loop.playbooks import playbook_short
from baibai_loop.screening.providers.jquants import JQuantsDailyBar

from .io import diff_jsonl, upsert_jsonl
from .records import PaperLedgerRecord, SkippedLedgerRecord, Tracking
from .tracking import resolve_price_on_or_before, resolve_tracking_prices

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_LATEST_PRICE_RE = re.compile(r"最新 adj close \([^)]*\) \| ([0-9,]+(?:\.[0-9]+)?) 円")


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
) -> SyncResult:
    screened = _load_screened(root)
    paper_records: list[dict[str, Any]] = []
    skipped_records: list[dict[str, Any]] = []
    warnings: list[str] = []
    research_keys: set[tuple[str, str]] = set()
    for path in sorted((root / "research").rglob("*.md")):
        parsed = _parse_research(path)
        if parsed is None:
            warnings.append(f"skip malformed research file: {path}")
            continue
        front, body = parsed
        decision = str(front.get("decision", "pending"))
        ticker = str(front["ticker"])
        playbook = str(front["playbook"])
        research_keys.add((ticker, playbook))
        screened_ref = str(front["screened_ref"])
        candidate = screened.get(screened_ref, {}).get(ticker, {})
        decision_date = _decision_date(path, front)
        baseline_price, adjustment_applied = _baseline_price(ticker, decision_date, body, bars)
        avg_turnover = _float_or_none(candidate.get("avg_turnover_oku")) or _extract_avg_turnover(
            body
        )
        position_size = _float_or_none(front.get("position_size_oku"))
        adv_participation = (
            round(position_size / avg_turnover * 100, 4)
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
        asof_date = _asof_date(screened_ref)
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
                screened_ref=screened_ref,
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
                screened_ref=screened_ref,
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
        skipped_records.extend(_load_select_skipped(root, screened, research_keys, warnings))
    else:
        warnings.append("select/ does not exist; skipped candidate-only ledger population")
    paper_by_month = _group_by_month(paper_records)
    skipped_by_month = _group_by_month(skipped_records)
    diff_lines: list[str] = []
    for month, records in paper_by_month.items():
        path = root / "ledger" / "paper" / f"{month}.jsonl"
        if dry_run:
            diff_lines.extend(diff_jsonl(path, records))
        else:
            upsert_jsonl(path, records)
    for month, records in skipped_by_month.items():
        path = root / "ledger" / "skipped" / f"{month}.jsonl"
        if dry_run:
            diff_lines.extend(diff_jsonl(path, records))
        else:
            upsert_jsonl(path, records)
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


def _load_screened(root: Path) -> dict[str, dict[str, Mapping[str, Any]]]:
    loaded: dict[str, dict[str, Mapping[str, Any]]] = {}
    for path in sorted((root / "screened").rglob("*.yaml")):
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
    screened: Mapping[str, Mapping[str, Mapping[str, Any]]],
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
        screened_ref = str(document.get("screened_ref") or "")
        candidates = _select_candidates(document)
        for candidate in candidates:
            ticker_raw = candidate.get("ticker")
            if not isinstance(ticker_raw, str):
                continue
            ticker = ticker_raw
            playbook = str(candidate.get("playbook") or document.get("playbook") or "default")
            if (ticker, playbook) in research_keys:
                continue
            screened_candidate = screened.get(screened_ref, {}).get(ticker, {})
            short = playbook_short(playbook) if playbook != "default" else "default"
            record = SkippedLedgerRecord(
                ledger_id=f"skipped-{decision_date:%Y%m%d}-{ticker}-{short}",
                ticker=ticker,
                name=str(candidate.get("name") or screened_candidate.get("name") or ""),
                decision="skipped",
                playbook=playbook,
                screened_ref=screened_ref,
                research_ref=None,
                asof_date=_asof_date(screened_ref) if screened_ref else decision_date.isoformat(),
                decision_date=decision_date.isoformat(),
                baseline_price=None,
                market_cap_oku=_float_or_none(screened_candidate.get("market_cap_oku")),
                avg_turnover_oku=_float_or_none(screened_candidate.get("avg_turnover_oku")),
                threshold_hit_count=len(screened_candidate.get("threshold_hit", []))
                if isinstance(screened_candidate.get("threshold_hit"), list)
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
        return date.fromisoformat(published[:10])
    return date.fromisoformat(path.name[:10])


def _asof_date(screened_ref: str) -> str:
    return Path(screened_ref).stem[:10]


def _baseline_price(
    ticker: str,
    decision_date: date,
    body: str,
    bars: tuple[JQuantsDailyBar, ...],
) -> tuple[float | None, bool]:
    if bars:
        price, adjusted = resolve_price_on_or_before(ticker, decision_date, bars)
        if price is not None:
            return price, adjusted
    match = _LATEST_PRICE_RE.search(body)
    if match:
        return float(match.group(1).replace(",", "")), False
    return None, False


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
