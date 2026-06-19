from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, TypeIs

from baibai_loop.coerce import parse_iso_date
from baibai_loop.yaml_io import safe_load

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """Minimal view of a trade record needed for forward review and benchmark.

    Sourced from `records/06-trades/**/*.md` front matter. Entry date / price are
    derived from the recorded executions so that forward horizons and P&L use the
    same basis as the trade contract.
    """

    trade_id: str
    ticker: str
    name: str
    position_state: str
    review_state: str
    entry_date: date
    quantity: int
    entry_price: float
    playbook_id: str | None = None
    # Remaining shares after partial exits; forward review / benchmark keep the
    # entry basis, while exposure sizes risk on what is still held.
    current_quantity: int | None = None
    # Cohort tag for forward-measurement filtering. ``None`` for the standard
    # workflow; back-fill / discretionary-override entries get an explicit
    # provenance label (e.g. ``pre_refactor_backfill``,
    # ``user_position_confirmed_after_screening``) so the benchmark CLI can
    # report the regulated cohort separately from the mixed total.
    cohort_tag: str | None = None


def load_open_trades(root: Path) -> list[TradeRecord]:
    """Load `position_state: open` trades from the trade records tree.

    Records without parseable executions are skipped; the forward review and
    benchmark commands only act on positions with a known entry basis.
    """
    trades: list[TradeRecord] = []
    for path in sorted((root / "records/06-trades").rglob("*.md")):
        front = _read_front_matter(path)
        if front is None:
            continue
        if front.get("position_state") != "open":
            continue
        record = _build_trade_record(front)
        if record is not None:
            trades.append(record)
    return trades


def _read_front_matter(path: Path) -> Mapping[str, Any] | None:
    match = _FRONT_MATTER_RE.match(path.read_text(encoding="utf-8"))
    if match is None:
        return None
    payload = safe_load(match.group(1))
    if not isinstance(payload, dict):
        return None
    return payload


def _build_trade_record(front: Mapping[str, Any]) -> TradeRecord | None:
    trade_id = front.get("trade_id")
    ticker = front.get("ticker")
    if not isinstance(trade_id, str) or not isinstance(ticker, str):
        return None
    entry = _entry_basis(front)
    if entry is None:
        return None
    entry_date, quantity, entry_price = entry
    playbook_id = front.get("playbook_id")
    current_quantity = front.get("current_quantity")
    cohort_tag = front.get("cohort_tag")
    return TradeRecord(
        trade_id=trade_id,
        ticker=ticker,
        name=str(front.get("name", "")),
        position_state=str(front.get("position_state", "")),
        review_state=str(front.get("review_state", "")),
        entry_date=entry_date,
        quantity=quantity,
        entry_price=entry_price,
        playbook_id=playbook_id if isinstance(playbook_id, str) else None,
        current_quantity=(
            current_quantity
            if isinstance(current_quantity, int) and not isinstance(current_quantity, bool)
            else None
        ),
        cohort_tag=cohort_tag if isinstance(cohort_tag, str) else None,
    )


def _entry_basis(front: Mapping[str, Any]) -> tuple[date, int, float] | None:
    executions = front.get("executions")
    if not isinstance(executions, Sequence) or isinstance(executions, str | bytes):
        return None
    buys = [
        execution
        for execution in executions
        if isinstance(execution, Mapping) and execution.get("side") == "buy"
    ]
    if not buys:
        return None
    entry_date: date | None = None
    total_quantity = 0
    notional = 0.0
    for execution in buys:
        traded_at = parse_iso_date(execution.get("at"))
        quantity = execution.get("quantity")
        price = execution.get("price_yen")
        if traded_at is None or not isinstance(quantity, int) or not _is_number(price):
            return None
        entry_date = traded_at if entry_date is None else min(entry_date, traded_at)
        total_quantity += quantity
        notional += float(price) * quantity
    if entry_date is None or total_quantity <= 0:
        return None
    return entry_date, total_quantity, notional / total_quantity


def _is_number(value: object) -> TypeIs[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)
