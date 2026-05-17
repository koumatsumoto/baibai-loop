from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .coverage import check_coverage
from .db import open_connection


class BriefDBCoverageError(RuntimeError):
    """Raised when a generated output would use incomplete quantitative data."""


@dataclass(frozen=True)
class ObservationView:
    indicator_id: str
    name: str
    domain: str
    geography: str
    unit: str | None
    as_of_date: str | None
    value_num: float | None
    value_text: str | None
    source_id: str | None


def generate_weekly(db_path: Path, *, start: date, end: date) -> dict[str, Any]:
    coverage = check_coverage(db_path, kind="world-weekly", start=start, end=end)
    if not coverage.ok:
        messages = "; ".join(finding.message for finding in coverage.findings)
        raise BriefDBCoverageError(messages)

    conn = open_connection(db_path)
    try:
        observations = tuple(_required_observations(conn, "world-weekly", end))
        payload: dict[str, Any] = {
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "layers": {
                "world": {"market_indicators": []},
                "japan": {"fx": []},
                "japan_equity": {},
            },
            "deltas": {"threshold_breaches": []},
        }
        breaches: list[dict[str, Any]] = []
        for observation in observations:
            previous = _previous_observation(conn, observation)
            item = _render_indicator(observation, previous)
            if observation.domain == "fx":
                payload["layers"]["japan"]["fx"].append(item)
            else:
                payload["layers"]["world"]["market_indicators"].append(item)
            breach = _threshold_breach(observation, previous)
            if breach is not None:
                breaches.append(breach)
        payload["deltas"]["threshold_breaches"] = breaches
        return payload
    finally:
        conn.close()


def _required_observations(
    conn: sqlite3.Connection,
    kind: str,
    end: date,
) -> list[ObservationView]:
    rows = conn.execute(
        "SELECT r.indicator_id, r.max_staleness_days, i.name, i.domain, i.geography, i.unit "
        "FROM coverage_requirements r "
        "JOIN indicators i ON i.indicator_id = r.indicator_id "
        "WHERE r.kind = ? AND r.required = 1 "
        "ORDER BY CASE i.domain "
        "WHEN 'rates' THEN 1 WHEN 'volatility' THEN 2 WHEN 'energy' THEN 3 WHEN 'fx' THEN 4 "
        "ELSE 9 END, r.indicator_id",
        (kind,),
    ).fetchall()
    observations: list[ObservationView] = []
    for row in rows:
        earliest = end - timedelta(days=int(row["max_staleness_days"]))
        observation = conn.execute(
            "SELECT * FROM indicator_observations "
            "WHERE indicator_id = ? AND fetch_status = 'ok' "
            "AND date(COALESCE(as_of_date, period_end, release_date)) BETWEEN date(?) AND date(?) "
            "ORDER BY date(COALESCE(as_of_date, period_end, release_date)) DESC, vintage_at DESC "
            "LIMIT 1",
            (str(row["indicator_id"]), earliest.isoformat(), end.isoformat()),
        ).fetchone()
        if observation is None:
            continue
        observations.append(
            ObservationView(
                indicator_id=str(row["indicator_id"]),
                name=str(row["name"]),
                domain=str(row["domain"]),
                geography=str(row["geography"]),
                unit=row["unit"] if isinstance(row["unit"], str) else None,
                as_of_date=(
                    observation["as_of_date"]
                    or observation["period_end"]
                    or observation["release_date"]
                ),
                value_num=(
                    float(observation["value_num"])
                    if observation["value_num"] is not None
                    else None
                ),
                value_text=(
                    str(observation["value_text"])
                    if observation["value_text"] is not None
                    else None
                ),
                source_id=(
                    str(observation["source_id"]) if observation["source_id"] is not None else None
                ),
            )
        )
    return observations


def _previous_observation(
    conn: sqlite3.Connection,
    current: ObservationView,
) -> ObservationView | None:
    if current.as_of_date is None:
        return None
    row = conn.execute(
        "SELECT o.*, i.name, i.domain, i.geography, i.unit "
        "FROM indicator_observations o "
        "JOIN indicators i ON i.indicator_id = o.indicator_id "
        "WHERE o.indicator_id = ? AND o.fetch_status = 'ok' "
        "AND date(COALESCE(o.as_of_date, o.period_end, o.release_date)) < date(?) "
        "ORDER BY date(COALESCE(o.as_of_date, o.period_end, o.release_date)) DESC, "
        "o.vintage_at DESC LIMIT 1",
        (current.indicator_id, current.as_of_date),
    ).fetchone()
    if row is None:
        return None
    return ObservationView(
        indicator_id=current.indicator_id,
        name=str(row["name"]),
        domain=str(row["domain"]),
        geography=str(row["geography"]),
        unit=row["unit"] if isinstance(row["unit"], str) else None,
        as_of_date=row["as_of_date"] or row["period_end"] or row["release_date"],
        value_num=float(row["value_num"]) if row["value_num"] is not None else None,
        value_text=str(row["value_text"]) if row["value_text"] is not None else None,
        source_id=str(row["source_id"]) if row["source_id"] is not None else None,
    )


def _render_indicator(
    observation: ObservationView,
    previous: ObservationView | None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": observation.name,
        "value": observation.value_text
        if observation.value_text is not None
        else observation.value_num,
    }
    if observation.as_of_date is not None:
        item["as_of"] = observation.as_of_date
    if previous is not None:
        item["wow_comment"] = _change_comment(observation, previous)
    if observation.source_id is not None:
        item["source_ids"] = [observation.source_id]
    return item


def _change_comment(current: ObservationView, previous: ObservationView) -> str:
    if current.value_num is None or previous.value_num is None:
        return "previous comparable value exists but numeric delta is unavailable"
    delta = current.value_num - previous.value_num
    if current.unit == "percent" and current.domain == "rates":
        return f"previous {previous.value_text} から {delta * 100:+.0f}bp"
    if current.unit == "bp":
        return f"previous {previous.value_text} から {delta:+.0f}bp"
    if previous.value_num:
        pct = delta / previous.value_num * 100
        return f"previous {previous.value_text} から {pct:+.1f}%"
    return f"previous {previous.value_text} から {delta:+.2f}"


def _threshold_breach(
    current: ObservationView,
    previous: ObservationView | None,
) -> dict[str, Any] | None:
    if previous is None or current.value_num is None or previous.value_num is None:
        return None
    delta = current.value_num - previous.value_num
    severity: str | None = None
    change: str
    if current.unit == "percent" and current.domain == "rates":
        bp = delta * 100
        severity = "major" if abs(bp) >= 15 else None
        change = f"{bp:+.0f}bp"
    elif current.unit == "bp":
        severity = "major" if abs(delta) >= 15 else None
        change = f"{delta:+.0f}bp"
    elif previous.value_num:
        pct = delta / previous.value_num * 100
        if abs(pct) >= 5:
            severity = "major"
        elif abs(pct) >= 3:
            severity = "notable"
        change = f"{pct:+.1f}%"
    else:
        return None
    if severity is None:
        return None
    return {
        "severity": severity,
        "indicator": current.name,
        "from": previous.value_text,
        "to": current.value_text,
        "change": change,
    }
