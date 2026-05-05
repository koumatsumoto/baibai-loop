from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .schema import FinancialSnapshot, FreshnessWarning, normalize_ticker


@dataclass(frozen=True, slots=True)
class DisclosureEvent:
    ticker: str
    disclosed_at: date
    title: str
    source: str
    url: str | None = None


@dataclass(frozen=True, slots=True)
class DisclosureLoadResult:
    events_by_ticker: Mapping[str, tuple[DisclosureEvent, ...]]
    file_count: int
    event_count: int
    skipped_record_count: int = 0
    unsupported_record_count: int = 0
    load_errors: tuple[str, ...] = ()


_EVENT_KIND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("borrowing", re.compile(r"借入|資金の借入|コミットメントライン|融資|ローン")),
    ("bond_issuance", re.compile(r"社債|新株予約権付社債|CB")),
    ("share_buyback", re.compile(r"自己株式取得|自己株式の取得|自己株買い")),
    ("capex", re.compile(r"設備投資|固定資産の取得|投資計画|工場建設")),
    ("equity_financing", re.compile(r"増資|第三者割当|公募|株式発行|新株式")),
    ("capital_reduction", re.compile(r"減資|資本金の額の減少")),
    ("capital_alliance", re.compile(r"資本業務提携|業務提携")),
    (
        "m_and_a",
        re.compile(r"M&A|買収|子会社化|株式取得|持分取得|譲受|事業譲受|吸収合併|合併"),
    ),
)


def load_disclosure_events(root: Path, *, asof_date: date) -> DisclosureLoadResult:
    if not root.exists():
        return DisclosureLoadResult(events_by_ticker={}, file_count=0, event_count=0)

    grouped: dict[str, list[DisclosureEvent]] = {}
    skipped_record_count = 0
    unsupported_record_count = 0
    load_errors: list[str] = []
    files = sorted(path for path in root.rglob("*.json") if path.is_file())
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            load_errors.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        records, unsupported = _iter_records(payload)
        unsupported_record_count += unsupported
        for record in records:
            event = _normalize_event(record, default_source=path.stem)
            if event is None:
                skipped_record_count += 1
                continue
            if event.disclosed_at > asof_date:
                continue
            if _event_kind(event.title) is None:
                continue
            grouped.setdefault(event.ticker, []).append(event)

    events_by_ticker = {
        ticker: tuple(sorted(events, key=lambda item: (item.disclosed_at, item.title)))
        for ticker, events in grouped.items()
    }
    return DisclosureLoadResult(
        events_by_ticker=events_by_ticker,
        file_count=len(files),
        event_count=sum(len(events) for events in events_by_ticker.values()),
        skipped_record_count=skipped_record_count,
        unsupported_record_count=unsupported_record_count,
        load_errors=tuple(load_errors),
    )


def detect_edinet_freshness_warnings(
    *,
    ticker: str,
    financial: FinancialSnapshot,
    events_by_ticker: Mapping[str, Sequence[DisclosureEvent]],
    asof_date: date,
) -> tuple[FreshnessWarning, ...]:
    source_submit_date = _parse_submit_date(financial.edinet_source_submit_datetime)
    if source_submit_date is None:
        return ()

    warnings: list[FreshnessWarning] = []
    for event in events_by_ticker.get(ticker, ()):
        if not (source_submit_date <= event.disclosed_at <= asof_date):
            continue
        event_kind = _event_kind(event.title)
        if event_kind is None:
            continue
        warnings.append(
            FreshnessWarning(
                source_family="edinet-metrics",
                stale_metric="edinet_metrics",
                reason="material_event_after_edinet_source",
                event_date=event.disclosed_at,
                event_kind=event_kind,
                event_title=event.title,
                event_source=event.source,
                event_url=event.url,
                edinet_source_submit_datetime=financial.edinet_source_submit_datetime,
            )
        )
    return tuple(warnings)


def _event_kind(title: str) -> str | None:
    for kind, pattern in _EVENT_KIND_PATTERNS:
        if pattern.search(title):
            return kind
    return None


def _iter_records(payload: object) -> tuple[tuple[Mapping[str, Any], ...], int]:
    if isinstance(payload, list):
        return tuple(item for item in payload if isinstance(item, Mapping)), sum(
            1 for item in payload if not isinstance(item, Mapping)
        )
    if isinstance(payload, Mapping):
        for key in ("events", "items", "records", "data", "disclosures"):
            value = payload.get(key)
            if isinstance(value, list):
                return tuple(item for item in value if isinstance(item, Mapping)), sum(
                    1 for item in value if not isinstance(item, Mapping)
                )
            if key in payload and value is not None:
                return (), 1
        return (payload,), 0
    return (), 1


def _normalize_event(
    record: Mapping[str, Any],
    *,
    default_source: str,
) -> DisclosureEvent | None:
    ticker = _parse_ticker(
        _coalesce(record, "ticker", "Ticker", "code", "Code", "LocalCode", "local_code")
    )
    title = _string(
        _coalesce(
            record,
            "title",
            "Title",
            "disclosure_title",
            "DisclosureTitle",
            "document_title",
            "DocumentTitle",
            "summary",
            "Summary",
        )
    )
    disclosed_at = _parse_date(
        _coalesce(
            record,
            "disclosed_at",
            "DisclosedAt",
            "disclosure_date",
            "DisclosureDate",
            "date",
            "Date",
            "published_at",
            "PublishedAt",
        )
    )
    if ticker is None or title is None or disclosed_at is None:
        return None
    source = _string(_coalesce(record, "source", "Source", "source_name", "SourceName"))
    url = _string(_coalesce(record, "url", "URL", "link", "Link"))
    return DisclosureEvent(
        ticker=ticker,
        disclosed_at=disclosed_at,
        title=title,
        source=source or default_source,
        url=url,
    )


def _parse_ticker(value: object) -> str | None:
    if value in (None, ""):
        return None
    raw = str(value).strip().upper()
    if len(raw) == 5 and raw[:4].isalnum():
        raw = raw[:4]
    try:
        return normalize_ticker(raw)
    except ValueError:
        return None


def _parse_submit_date(value: str | None) -> date | None:
    if value in (None, ""):
        return None
    return _parse_date(value)


def _parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _coalesce(record: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def _string(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip() or None
