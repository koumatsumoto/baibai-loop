"""Validator callable allow-list used by policy and trade checks."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from .errors import ValidationFinding

KNOWN_VALIDATOR_CALLABLE_IDS = {
    "earnings_straddle_window",
    "boj_eve_window",
    "fomc_eve_window",
    "no_margin_trading",
}
ValidatorCallable = Callable[[Mapping[str, Any], list[Mapping[str, Any]]], bool]
KILL_SWITCH_IMPLEMENTATIONS: dict[str, ValidatorCallable] = {}
_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def _kill_switch(callable_id: str) -> Callable[[ValidatorCallable], ValidatorCallable]:
    def decorator(func: ValidatorCallable) -> ValidatorCallable:
        KILL_SWITCH_IMPLEMENTATIONS[callable_id] = func
        return func

    return decorator


def has_validator_callable(callable_id: str) -> bool:
    return callable_id in KILL_SWITCH_IMPLEMENTATIONS


def evaluate_kill_switch(
    callable_id: str,
    context: Mapping[str, Any],
    events: list[Mapping[str, Any]],
) -> bool | None:
    implementation = KILL_SWITCH_IMPLEMENTATIONS.get(callable_id)
    if implementation is None:
        return None
    return implementation(context, events)


def validate_callable_ids_file(path: Path) -> list[ValidationFinding]:
    try:
        text = path.read_text(encoding="utf-8")
        match = _FRONT_MATTER_RE.match(text)
        raw = yaml.safe_load(match.group(1) if match else text)
    except (OSError, yaml.YAMLError) as exc:
        return [
            ValidationFinding(
                severity="error",
                target=path,
                code="registry.parse",
                message=f"failed to read callable-id source: {exc}",
            )
        ]
    findings: list[ValidationFinding] = []
    _walk(path, raw, findings, ())
    return findings


def _walk(
    path: Path,
    value: object,
    findings: list[ValidationFinding],
    parts: tuple[str, ...],
) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            next_parts = (*parts, str(key))
            if key == "validator_callable_id":
                if child not in KNOWN_VALIDATOR_CALLABLE_IDS:
                    findings.append(
                        ValidationFinding(
                            severity="error",
                            target=path,
                            code="registry.unknown-callable",
                            message=f"unknown validator_callable_id: {child!r}",
                            location=".".join(next_parts),
                        )
                    )
                elif not has_validator_callable(str(child)):
                    findings.append(
                        ValidationFinding(
                            severity="error",
                            target=path,
                            code="registry.missing-implementation",
                            message=f"validator_callable_id has no implementation: {child!r}",
                            location=".".join(next_parts),
                        )
                    )
            else:
                _walk(path, child, findings, next_parts)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk(path, child, findings, (*parts, f"[{index}]"))


@_kill_switch("earnings_straddle_window")
def _earnings_straddle_window(context: Mapping[str, Any], events: list[Mapping[str, Any]]) -> bool:
    ticker = str(context.get("ticker") or "")
    at = _context_date(context)
    window_days = _window_days(context)
    if at is None:
        return False
    for event in events:
        if event.get("kind") != "earnings":
            continue
        if ticker and not _event_matches_ticker(event, ticker):
            continue
        event_date = _event_date(event)
        if event_date is not None and abs((event_date - at).days) <= window_days:
            return True
    return False


@_kill_switch("boj_eve_window")
def _boj_eve_window(context: Mapping[str, Any], events: list[Mapping[str, Any]]) -> bool:
    return _event_eve_window(context, events, "boj")


@_kill_switch("fomc_eve_window")
def _fomc_eve_window(context: Mapping[str, Any], events: list[Mapping[str, Any]]) -> bool:
    return _event_eve_window(context, events, "fomc")


@_kill_switch("no_margin_trading")
def _no_margin_trading(context: Mapping[str, Any], events: list[Mapping[str, Any]]) -> bool:
    _ = events
    return context.get("uses_margin") is True


def _event_eve_window(
    context: Mapping[str, Any], events: list[Mapping[str, Any]], kind: str
) -> bool:
    at = _context_date(context)
    window_days = _window_days(context)
    if at is None:
        return False
    for event in events:
        event_kind = str(event.get("kind") or event.get("event_kind") or "").lower()
        event_id = str(event.get("event_id") or "").lower()
        if kind not in event_kind and kind not in event_id:
            continue
        event_date = _event_date(event)
        if event_date is None:
            continue
        days_until = (event_date - at).days
        if 0 <= days_until <= window_days:
            return True
    return False


def _context_date(context: Mapping[str, Any]) -> date | None:
    at = context.get("at")
    if isinstance(at, datetime):
        return at.date()
    if isinstance(at, str):
        try:
            return datetime.fromisoformat(at).date()
        except ValueError:
            return None
    return None


def _event_date(event: Mapping[str, Any]) -> date | None:
    value = event.get("date") or event.get("event_date") or event.get("at")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _window_days(context: Mapping[str, Any]) -> int:
    value = context.get("window_days")
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 1


def _event_matches_ticker(event: Mapping[str, Any], ticker: str) -> bool:
    event_ticker = event.get("ticker")
    if isinstance(event_ticker, str):
        return event_ticker == ticker
    event_id = event.get("event_id")
    return isinstance(event_id, str) and event_id.startswith(f"{ticker}-")
