"""Place today's candidate supply and breadth inside their own histories.

The scorecard separates supply (how much expected return is available) from breadth
(how many distinct tickers and economic clusters carry it).  It is a read-only
diagnostic, not a screening signal, regime label, or selection input.

Missing panel months, retained runs, longlist history, and shortlist judgments are
reported as unmeasured.  A missing observation is never imputed as zero.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from pathlib import Path
from statistics import fmean
from typing import TextIO

import yaml
from tools.experiments.measure_signal_cohorts import (
    SignalCohortMeasurementError,
    require_single_rules_hash,
)

DEFAULT_CALIBRATION_DIR = Path("stores/screening/calibration")
DEFAULT_RUNS_DB = Path("stores/screening/runs.sqlite")
DEFAULT_APPLICATION_DB = Path("stores/application/baibai.sqlite")
# selection.liquidity と同じ関門。method/screening/rules の値と揃える。
MIN_MARKET_CAP_OKU = 100.0
MIN_AVG_TURNOVER_OKU = 1.0
MIN_LISTING_SPAN_DAYS = 182.0
MIN_PANEL_POPULATION = 100
SUPPLY_TOP_N = 5
BREADTH_TOP_N = 20
TRAILING_MONTHS = 12
DEFAULT_HURDLE = 0.085


class SupplyContextError(ValueError):
    """Raised when the stores cannot support a truthful comparison."""


@dataclass(frozen=True)
class BreadthSnapshot:
    asof: date
    tickers: frozenset[str]
    carry_dominant_share: float
    sector_hhi: float
    max_cluster_share: float
    largest_cluster: str


@dataclass(frozen=True)
class CurrentSelection:
    selection_id: str
    run_asof: date
    top5_mean_er: float
    breadth: BreadthSnapshot | None
    breadth_reason: str | None


def _optional_float(value: object) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if not isinstance(value, str | int | float):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _optional_rank(value: object) -> int | None:
    parsed = _optional_float(value)
    if parsed is None or not parsed.is_integer() or parsed < 1:
        return None
    return int(parsed)


def _percentile_rank(history: Sequence[float], value: float) -> float:
    """Return the share of prior observations strictly below ``value``."""

    below = sum(1 for item in history if item < value)
    return round(below / len(history) * 100, 1)


def _history_median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _next_month(value: date) -> tuple[int, int]:
    if value.month == 12:
        return value.year + 1, 1
    return value.year, value.month + 1


def _months_are_consecutive(earlier: date, later: date) -> bool:
    return (later.year, later.month) == _next_month(earlier)


def _breadth_snapshot(
    asof: date, rows: Sequence[Mapping[str, object]]
) -> tuple[BreadthSnapshot | None, str | None]:
    by_rank: dict[int, Mapping[str, object]] = {}
    for row in rows:
        rank = _optional_rank(row.get("selection_rank", row.get("rank")))
        if rank is None or rank > BREADTH_TOP_N:
            continue
        if rank in by_rank:
            return None, "top20_ranks_are_not_unique"
        by_rank[rank] = row
    if set(by_rank) != set(range(1, BREADTH_TOP_N + 1)):
        return None, "top20_is_incomplete"

    tickers: set[str] = set()
    sectors: list[str] = []
    clusters: list[str] = []
    carry_dominant = 0
    for rank in range(1, BREADTH_TOP_N + 1):
        row = by_rank[rank]
        ticker = str(row.get("ticker") or "")
        sector = str(row.get("sector_33") or "")
        carry = _optional_float(row.get("er_carry_annual"))
        reversion = _optional_float(row.get("er_reversion_annual"))
        if not ticker or not sector or carry is None or reversion is None:
            return None, "top20_breadth_inputs_are_incomplete"
        if ticker in tickers:
            return None, "top20_tickers_are_not_unique"
        tickers.add(ticker)
        sectors.append(sector)
        dominance = "carry" if carry > reversion else "reversion"
        carry_dominant += dominance == "carry"
        clusters.append(f"{sector} × {dominance}")

    sector_counts = Counter(sectors)
    cluster_counts = Counter(clusters)
    largest_cluster, largest_cluster_count = sorted(
        cluster_counts.items(), key=lambda item: (-item[1], item[0])
    )[0]
    return (
        BreadthSnapshot(
            asof=asof,
            tickers=frozenset(tickers),
            carry_dominant_share=carry_dominant / BREADTH_TOP_N,
            sector_hhi=sum((count / BREADTH_TOP_N) ** 2 for count in sector_counts.values()),
            max_cluster_share=largest_cluster_count / BREADTH_TOP_N,
            largest_cluster=largest_cluster,
        ),
        None,
    )


def _panel_history(
    calibration_dir: Path, *, hurdle: float
) -> tuple[
    list[tuple[str, float]], list[tuple[str, int]], list[BreadthSnapshot], list[dict[str, str]]
]:
    top5: list[tuple[str, float]] = []
    counts: list[tuple[str, int]] = []
    breadth: list[BreadthSnapshot] = []
    degraded: list[dict[str, str]] = []
    paths = sorted(calibration_dir.glob("panel-*.csv"))
    if not paths:
        raise SupplyContextError(f"no panel rows under {calibration_dir}")
    previous_asof: date | None = None
    for path in paths:
        asof_text = path.name.removeprefix("panel-").removesuffix(".csv")
        try:
            asof = date.fromisoformat(asof_text)
        except ValueError as error:
            raise SupplyContextError(f"panel filename has an invalid as-of: {path}") from error
        if previous_asof is not None and not _months_are_consecutive(previous_asof, asof):
            degraded.append(
                {
                    "as_of": asof_text,
                    "reason": f"panel_month_gap_after_{previous_asof.isoformat()}",
                }
            )
        previous_asof = asof
        ranked: list[tuple[int, float]] = []
        clearing = 0
        rows: list[dict[str, object]] = []
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                rows.append(dict(row))
                estimate = _optional_float(row.get("er_annual"))
                if estimate is None:
                    continue
                rank = _optional_rank(row.get("selection_rank"))
                if rank is not None:
                    ranked.append((rank, estimate))
                if (
                    row.get("pass_screen") in {"True", "true", "1"}
                    and estimate >= hurdle
                    and (_optional_float(row.get("market_cap_oku")) or 0.0) >= MIN_MARKET_CAP_OKU
                    and (_optional_float(row.get("avg_turnover_oku")) or 0.0)
                    >= MIN_AVG_TURNOVER_OKU
                    and (_optional_float(row.get("listing_span_days")) or 0.0)
                    >= MIN_LISTING_SPAN_DAYS
                ):
                    clearing += 1
        ranked.sort()
        if len(ranked) >= SUPPLY_TOP_N:
            top5.append((asof_text, fmean(estimate for _, estimate in ranked[:SUPPLY_TOP_N])))
        counts.append((asof_text, clearing))
        population_count = sum(row.get("in_population") in {"True", "true", "1"} for row in rows)
        snapshot: BreadthSnapshot | None
        breadth_reason: str | None
        if population_count < MIN_PANEL_POPULATION:
            snapshot = None
            breadth_reason = f"in_population_below_{MIN_PANEL_POPULATION}"
        else:
            snapshot, breadth_reason = _breadth_snapshot(asof, rows)
        if snapshot is None:
            degraded.append({"as_of": asof_text, "reason": breadth_reason or "breadth_unmeasured"})
        else:
            breadth.append(snapshot)
    if not top5:
        raise SupplyContextError("no panel carries a selection ranking")
    return top5, counts, breadth, degraded


def _selection_longlist(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, Mapping):
        return []
    raw = payload.get("longlist")
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        return []
    return [dict(item) for item in raw]


def _candidate_breadth_rows(
    connection: sqlite3.Connection,
    *,
    run_revision_id: str,
    longlist: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in longlist:
        ticker = str(item.get("ticker") or "")
        rank = _optional_rank(item.get("rank"))
        if not ticker or rank is None or rank > BREADTH_TOP_N:
            continue
        candidate = connection.execute(
            "SELECT sector_33, payload FROM screening_candidate "
            "WHERE run_revision_id = ? AND ticker = ?",
            (run_revision_id, ticker),
        ).fetchone()
        if candidate is None:
            continue
        payload = json.loads(str(candidate["payload"]))
        metrics = payload.get("metrics") if isinstance(payload, Mapping) else None
        rows.append(
            {
                "rank": rank,
                "ticker": ticker,
                "sector_33": candidate["sector_33"],
                "er_carry_annual": metrics.get("er_carry_annual")
                if isinstance(metrics, Mapping)
                else None,
                "er_reversion_annual": (
                    metrics.get("er_reversion_annual") if isinstance(metrics, Mapping) else None
                ),
            }
        )
    return rows


def _current_selection(runs_db: Path, *, selection_id: str | None) -> CurrentSelection:
    if not runs_db.exists():
        raise SupplyContextError(f"run store is unavailable: {runs_db}")
    connection = sqlite3.connect(f"file:{runs_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if selection_id is None:
            row = connection.execute(
                "SELECT s.selection_id, s.run_revision_id, s.payload, r.asof_date "
                "FROM screening_selection s JOIN screening_run r USING (run_revision_id) "
                "ORDER BY s.created_at DESC LIMIT 1"
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT s.selection_id, s.run_revision_id, s.payload, r.asof_date "
                "FROM screening_selection s JOIN screening_run r USING (run_revision_id) "
                "WHERE s.selection_id = ? ORDER BY s.created_at DESC LIMIT 1",
                (selection_id,),
            ).fetchone()
        if row is None:
            raise SupplyContextError("run store carries no matching selection")
        longlist = _selection_longlist(json.loads(str(row["payload"])))
        estimates: list[float | None] = []
        for item in longlist[:SUPPLY_TOP_N]:
            candidate = connection.execute(
                "SELECT er_annual FROM screening_candidate "
                "WHERE run_revision_id = ? AND ticker = ?",
                (row["run_revision_id"], str(item.get("ticker") or "")),
            ).fetchone()
            estimates.append(None if candidate is None else _optional_float(candidate["er_annual"]))
        if len(estimates) < SUPPLY_TOP_N or any(value is None for value in estimates):
            raise SupplyContextError(
                "selection carries fewer than 5 ranked candidates with an estimate"
            )
        run_asof = date.fromisoformat(str(row["asof_date"]))
        breadth, breadth_reason = _breadth_snapshot(
            run_asof,
            _candidate_breadth_rows(
                connection,
                run_revision_id=str(row["run_revision_id"]),
                longlist=longlist,
            ),
        )
        return CurrentSelection(
            selection_id=str(row["selection_id"]),
            run_asof=run_asof,
            top5_mean_er=fmean(float(value) for value in estimates if value is not None),
            breadth=breadth,
            breadth_reason=breadth_reason,
        )
    finally:
        connection.close()


def _previous_top20_from_runs(
    runs_db: Path, *, before_asof: date
) -> tuple[date, frozenset[str]] | None:
    connection = sqlite3.connect(f"file:{runs_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT r.asof_date, s.payload FROM screening_selection s "
            "JOIN screening_run r USING (run_revision_id) WHERE r.asof_date < ? "
            "ORDER BY r.asof_date DESC, s.created_at DESC, s.selection_id DESC LIMIT 1",
            (before_asof.isoformat(),),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    longlist = _selection_longlist(json.loads(str(row["payload"])))
    ranked = {
        rank: str(item.get("ticker") or "")
        for item in longlist
        if (rank := _optional_rank(item.get("rank"))) is not None and rank <= BREADTH_TOP_N
    }
    if set(ranked) != set(range(1, BREADTH_TOP_N + 1)) or not all(ranked.values()):
        return None
    return date.fromisoformat(str(row["asof_date"])), frozenset(ranked.values())


def _previous_top20_from_history(
    history_dir: Path | None, *, before_asof: date
) -> tuple[date, frozenset[str]] | None:
    if history_dir is None or not history_dir.is_dir():
        return None
    records: list[tuple[date, Mapping[str, object]]] = []
    for path in sorted(history_dir.glob("*.json")):
        try:
            file_asof = date.fromisoformat(path.stem)
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
            raise SupplyContextError(f"longlist history is unreadable: {path}") from error
        if not isinstance(payload, Mapping) or str(payload.get("as_of")) != path.stem:
            raise SupplyContextError(f"longlist history filename and as_of differ: {path}")
        if file_asof < before_asof:
            records.append((file_asof, payload))
    for record_asof, record in reversed(records):
        if record.get("kind") != "daily-longlist-membership" or record.get("schema_version") != 1:
            raise SupplyContextError("longlist history has an unsupported contract")
        selection_status = record.get("selection_status")
        if selection_status == "selection_missing":
            continue
        if selection_status != "available":
            raise SupplyContextError("longlist history has an invalid selection status")
        members = record.get("members")
        if not isinstance(members, list) or not all(isinstance(item, Mapping) for item in members):
            continue
        ranked = {
            rank: str(item.get("ticker") or "")
            for item in members
            if (rank := _optional_rank(item.get("rank"))) is not None and rank <= BREADTH_TOP_N
        }
        if set(ranked) != set(range(1, BREADTH_TOP_N + 1)) or not all(ranked.values()):
            continue
        return record_asof, frozenset(ranked.values())
    return None


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    if not union:
        raise SupplyContextError("top20 Jaccard has an empty union")
    return len(left & right) / len(union)


def _historical_breadth(
    snapshots: Sequence[BreadthSnapshot],
    *,
    latest_panel_asof: date,
) -> tuple[dict[str, list[float]], list[dict[str, str]], dict[str, object]]:
    histories: dict[str, list[float]] = {
        "temporal_jaccard": [],
        "trailing_12m_unique_top20": [],
        "carry_dominant_share": [item.carry_dominant_share for item in snapshots],
        "sector_hhi": [item.sector_hhi for item in snapshots],
        "max_cluster_share": [item.max_cluster_share for item in snapshots],
    }
    degraded: list[dict[str, str]] = []
    by_month = {(item.asof.year, item.asof.month): item for item in snapshots}
    ordered = sorted(snapshots, key=lambda item: item.asof)
    for current in ordered:
        previous_year, previous_month = (
            (current.asof.year - 1, 12)
            if current.asof.month == 1
            else (current.asof.year, current.asof.month - 1)
        )
        previous = by_month.get((previous_year, previous_month))
        if previous is not None:
            histories["temporal_jaccard"].append(_jaccard(current.tickers, previous.tickers))
        elif current is not ordered[0]:
            degraded.append(
                {"as_of": current.asof.isoformat(), "reason": "previous_panel_month_unavailable"}
            )

    latest_trailing: dict[str, object] = {
        "status": "unmeasured",
        "basis": "latest_12_consecutive_month_end_panels",
        "value": None,
        "reason": "twelve_consecutive_panel_months_are_unavailable",
    }
    for index in range(TRAILING_MONTHS - 1, len(ordered)):
        window = ordered[index - TRAILING_MONTHS + 1 : index + 1]
        if not all(
            _months_are_consecutive(earlier.asof, later.asof) for earlier, later in pairwise(window)
        ):
            degraded.append(
                {"as_of": window[-1].asof.isoformat(), "reason": "trailing_12m_panel_gap"}
            )
            continue
        unique_count = len(set().union(*(item.tickers for item in window)))
        histories["trailing_12m_unique_top20"].append(float(unique_count))
        if window[-1].asof == latest_panel_asof:
            latest_trailing = {
                "status": "measured",
                "basis": "latest_12_consecutive_month_end_panels",
                "panel_asof": window[-1].asof.isoformat(),
                "value": unique_count,
                "reason": None,
            }
    return histories, degraded, latest_trailing


def _metric(
    *,
    value: float | int | None,
    basis: str,
    history: Sequence[float],
    reason: str | None = None,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "measured" if value is not None else "unmeasured",
        "basis": basis,
        "value": value,
        "reason": reason if value is None else None,
        "history_observations": len(history),
        "history_median": round(_history_median(history), 4) if history else None,
        "percentile_rank": (
            _percentile_rank(history, float(value)) if history and value is not None else None
        ),
        "percentile_status": "measured" if history and value is not None else "unmeasured",
    }
    if extra:
        result.update(extra)
    return result


def _shortlist_event_wait(
    application_db: Path, *, asof: date
) -> tuple[dict[str, object], list[float]]:
    if not application_db.exists():
        return (
            _metric(
                value=None,
                basis="latest_canonical_shortlist_rejections",
                history=[],
                reason="application_store_unavailable",
            ),
            [],
        )
    connection = sqlite3.connect(f"file:{application_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT shortlist_id, as_of, payload FROM shortlist WHERE as_of <= ? "
            "ORDER BY as_of, published_at, shortlist_id",
            (asof.isoformat(),),
        ).fetchall()
    finally:
        connection.close()
    latest_by_asof = {str(row["as_of"]): row for row in rows}
    if not latest_by_asof:
        return (
            _metric(
                value=None,
                basis="latest_canonical_shortlist_rejections",
                history=[],
                reason="no_canonical_shortlist",
            ),
            [],
        )
    observations: list[tuple[str, str, float, int, int]] = []
    for shortlist_asof, row in latest_by_asof.items():
        payload = json.loads(str(row["payload"]))
        entries = payload.get("entries") if isinstance(payload, Mapping) else None
        if not isinstance(entries, list):
            continue
        rejected = [
            item
            for item in entries
            if isinstance(item, Mapping) and item.get("decision") == "rejected"
        ]
        if not rejected:
            continue
        event_wait = sum(item.get("reject_class") == "event_wait" for item in rejected)
        observations.append(
            (
                str(row["shortlist_id"]),
                shortlist_asof,
                event_wait / len(rejected),
                event_wait,
                len(rejected),
            )
        )
    latest_asof, latest_row = list(latest_by_asof.items())[-1]
    latest_payload = json.loads(str(latest_row["payload"]))
    latest_entries = latest_payload.get("entries") if isinstance(latest_payload, Mapping) else None
    latest_rejected = (
        [
            item
            for item in latest_entries
            if isinstance(item, Mapping) and item.get("decision") == "rejected"
        ]
        if isinstance(latest_entries, list)
        else []
    )
    history = [item[2] for item in observations if item[1] < latest_asof]
    if not latest_rejected:
        return (
            _metric(
                value=None,
                basis="latest_canonical_shortlist_rejections",
                history=history,
                reason="latest_shortlist_has_no_rejected_entries",
                extra={
                    "shortlist_id": str(latest_row["shortlist_id"]),
                    "shortlist_asof": latest_asof,
                    "event_wait_count": None,
                    "rejected_count": 0,
                },
            ),
            history,
        )
    current = next(item for item in observations if item[1] == latest_asof)
    shortlist_id, shortlist_asof, value, event_wait, rejected_count = current
    return (
        _metric(
            value=round(value, 4),
            basis="latest_canonical_shortlist_rejections",
            history=history,
            extra={
                "shortlist_id": shortlist_id,
                "shortlist_asof": shortlist_asof,
                "event_wait_count": event_wait,
                "rejected_count": rejected_count,
            },
        ),
        history,
    )


def build_supply_context(
    *,
    calibration_dir: Path,
    runs_db: Path,
    application_db: Path = DEFAULT_APPLICATION_DB,
    longlist_history_dir: Path | None = None,
    selection_id: str | None,
    hurdle: float,
) -> dict[str, object]:
    top5_history, count_history, panel_breadth, panel_degraded = _panel_history(
        calibration_dir, hurdle=hurdle
    )
    current = _current_selection(runs_db, selection_id=selection_id)
    rules_hash = require_single_rules_hash(calibration_dir)
    latest_count_asof, latest_count = count_history[-1]
    top5_values = [value for _, value in top5_history]
    count_values = [float(value) for _, value in count_history[:-1]]
    if not count_values:
        raise SupplyContextError("a single panel cannot place its own count in history")

    histories, breadth_degraded, trailing = _historical_breadth(
        panel_breadth,
        latest_panel_asof=date.fromisoformat(count_history[-1][0]),
    )
    previous = _previous_top20_from_runs(runs_db, before_asof=current.run_asof)
    previous_source = "run_store"
    if previous is None:
        previous = _previous_top20_from_history(longlist_history_dir, before_asof=current.run_asof)
        previous_source = "longlist_history"
    if current.breadth is None or previous is None:
        temporal = _metric(
            value=None,
            basis="current_selection_top20_vs_previous_cycle_top20",
            history=histories["temporal_jaccard"],
            reason=(
                current.breadth_reason
                if current.breadth is None
                else "previous_cycle_top20_unavailable_after_run_retention"
            ),
        )
    else:
        previous_asof, previous_tickers = previous
        temporal = _metric(
            value=round(_jaccard(current.breadth.tickers, previous_tickers), 4),
            basis="current_selection_top20_vs_previous_cycle_top20",
            history=histories["temporal_jaccard"],
            extra={
                "previous_source": previous_source,
                "previous_asof": previous_asof.isoformat(),
            },
        )

    current_breadth = current.breadth
    carry = _metric(
        value=(None if current_breadth is None else round(current_breadth.carry_dominant_share, 4)),
        basis="current_selection_top20",
        history=histories["carry_dominant_share"],
        reason=current.breadth_reason,
    )
    sector_hhi = _metric(
        value=None if current_breadth is None else round(current_breadth.sector_hhi, 4),
        basis="current_selection_top20",
        history=histories["sector_hhi"],
        reason=current.breadth_reason,
    )
    cluster = _metric(
        value=None if current_breadth is None else round(current_breadth.max_cluster_share, 4),
        basis="current_selection_top20_sector33_x_dominance",
        history=histories["max_cluster_share"],
        reason=current.breadth_reason,
        extra={
            "largest_cluster": None if current_breadth is None else current_breadth.largest_cluster
        },
    )
    trailing_history = histories["trailing_12m_unique_top20"]
    if trailing.get("status") == "measured":
        trailing_history = trailing_history[:-1]
    raw_trailing_value = trailing.get("value")
    trailing_value = (
        raw_trailing_value
        if trailing.get("status") == "measured"
        and isinstance(raw_trailing_value, int | float)
        and not isinstance(raw_trailing_value, bool)
        else None
    )
    trailing_metric = _metric(
        value=trailing_value,
        basis=str(trailing["basis"]),
        history=trailing_history,
        reason=str(trailing.get("reason")) if trailing.get("status") != "measured" else None,
        extra={"panel_asof": trailing.get("panel_asof")},
    )
    event_wait, _event_wait_history = _shortlist_event_wait(application_db, asof=current.run_asof)

    return {
        "kind": "supply-context",
        "rules_hash": rules_hash,
        "hurdle_annual_ratio": hurdle,
        "history_panels": len(top5_history),
        "history_asof_start": top5_history[0][0],
        "history_asof_end": top5_history[-1][0],
        "selection_top5_mean_er": {
            "basis": "current_selection_top5",
            "selection_id": current.selection_id,
            "run_asof": current.run_asof.isoformat(),
            "value": round(current.top5_mean_er, 4),
            "history_median": round(_history_median(top5_values), 4),
            "percentile_rank": _percentile_rank(top5_values, current.top5_mean_er),
        },
        "hurdle_clearing_count": {
            "basis": "latest_month_end_panel",
            "panel_asof": latest_count_asof,
            "value": latest_count,
            "history_median": _history_median(count_values),
            "percentile_rank": _percentile_rank(count_values, float(latest_count)),
        },
        "breadth": {
            "temporal_jaccard": temporal,
            "trailing_12m_unique_top20": trailing_metric,
            "carry_dominant_share": carry,
            "sector_hhi": sector_hhi,
            "max_cluster_share": cluster,
            "event_wait_share": event_wait,
            "history_degraded": [*panel_degraded, *breadth_degraded],
        },
        "reading": (
            "供給と幅は別の座標で、逆を向くことがある。単一状態へ畳まない。"
            "ticker の新しさを KPI にせず、carry 集中も単独で悪化と判定しない。"
            "unmeasured は 0 や異常なしを意味しない。"
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.experiments.measure_supply_context",
        description=(
            "Report where today's candidate supply and breadth sit inside their histories. "
            "Read-only: never changes screening rules, estimates, selections, or stores."
        ),
    )
    parser.add_argument("--calibration-dir", type=Path, default=DEFAULT_CALIBRATION_DIR)
    parser.add_argument("--runs-db", type=Path, default=DEFAULT_RUNS_DB)
    parser.add_argument("--application-db", type=Path, default=DEFAULT_APPLICATION_DB)
    parser.add_argument("--longlist-history-dir", type=Path)
    parser.add_argument("--selection-id")
    parser.add_argument("--hurdle", type=float, default=DEFAULT_HURDLE)
    parser.add_argument("--out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = build_supply_context(
            calibration_dir=args.calibration_dir,
            runs_db=args.runs_db,
            application_db=args.application_db,
            longlist_history_dir=args.longlist_history_dir,
            selection_id=args.selection_id,
            hurdle=args.hurdle,
        )
    except (
        SupplyContextError,
        SignalCohortMeasurementError,
        json.JSONDecodeError,
        OSError,
        sqlite3.Error,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, file=stdout or sys.stdout, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
