"""Import J-Quants originals and replay affected saved scopes in receipt order."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from baibai_engine.data_collection.jquants.http import JQuantsAcquisitionError, JQuantsSource
from baibai_engine.data_collection.jquants.receipts import import_requests, read_complete_request
from baibai_engine.market.jquants_projection import project_request
from baibai_engine.machine_analysis.observations.trading_activity.margin_publication import LEGACY_WEEKLY_LAST_BALANCE_DATE
from baibai_engine.storage.observations.coverage import range_covered
from baibai_engine.storage.observations.schema import open_connection, validate_current_schema

MARKET_SOURCES: tuple[JQuantsSource, ...] = (
    "master",
    "daily_bars",
    "market_calendar",
    "margin_interest",
    "margin_alerts",
    "short_sale_reports",
)
FINANCIAL_SOURCES: tuple[JQuantsSource, ...] = ("financial_summaries",)


@dataclass(frozen=True, slots=True)
class _Scope:
    request_id: str
    source: str
    start: date
    end: date
    received_at: datetime

    @property
    def order(self) -> tuple[datetime, str]:
        return self.received_at, self.request_id


def _scope(
    request_id: str, source: str, params: Mapping[str, str], received_at: datetime
) -> _Scope:
    if source == "market_calendar" and set(params) == {"from", "to"}:
        first, last = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
    else:
        key = "disc_date" if source == "short_sale_reports" else "date"
        if set(params) != {key}:
            raise ValueError("J-Quants handoff requires a full-cross-section explicit scope")
        first = last = date.fromisoformat(params[key])
    if last < first:
        raise ValueError("J-Quants handoff range is reversed")
    return _Scope(request_id, source, first, last, received_at)


def export_handoff(
    source: Path, output: Path, *, since: datetime, sources: Sequence[JQuantsSource]
) -> tuple[str, ...]:
    if output.exists():
        raise FileExistsError(output)
    if since.tzinfo is None:
        raise ValueError("J-Quants handoff cutoff requires a timezone")
    allowed = set(MARKET_SOURCES + FINANCIAL_SOURCES)
    if not sources or not set(sources) <= allowed:
        raise ValueError("unsupported J-Quants handoff sources")
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original,
        closing(open_connection(output)) as target,
        target,
    ):
        validate_current_schema(original)
        original.execute("BEGIN")
        selected = tuple(
            sorted(
                {
                    row[0]
                    for name in sources
                    for row in original.execute(
                        "SELECT DISTINCT request_id FROM jquants_source_pages "
                        "WHERE source = ? AND received_at_utc >= ?",
                        (name, since.astimezone(UTC).isoformat(timespec="microseconds")),
                    )
                }
            )
        )
        import_requests(original, target, selected)
    return selected


def _headers(conn: sqlite3.Connection, sources: set[str]) -> list[_Scope]:
    result = []
    for request_id, source, query, received_at in conn.execute(
        "SELECT request_id, source, query_json, "
        "(SELECT MAX(p.received_at_utc) FROM jquants_source_pages p "
        "WHERE p.request_id = h.request_id) "
        "FROM jquants_source_pages h WHERE page_index = 0"
    ):
        if source not in sources:
            continue
        try:
            result.append(
                _scope(request_id, source, json.loads(query), datetime.fromisoformat(received_at))
            )
        except (KeyError, ValueError):
            # Retained filtered/manual queries are originals, not replaceable full scopes.
            continue
    return result


def _related(incoming: Sequence[_Scope], retained: Sequence[_Scope]) -> list[_Scope]:
    # Canonical projections already contain earlier receipts. Only a later receipt
    # can be rolled back by applying an incoming one. Walk forward in receipt order;
    # a selected newer calendar range may extend the affected interval to its tail.
    selected = list(incoming)
    incoming_ids = {scope.request_id for scope in incoming}
    for scope in sorted(retained, key=lambda item: item.order):
        if scope.request_id in incoming_ids:
            continue
        if any(
            previous.source == scope.source
            and previous.order < scope.order
            and previous.start <= scope.end
            and previous.end >= scope.start
            for previous in selected
        ):
            selected.append(scope)
    return selected


def _coverage_source(scope: _Scope) -> str:
    if scope.source == "margin_interest":
        return (
            "jquants_weekly_margin"
            if scope.start <= LEGACY_WEEKLY_LAST_BALANCE_DATE
            else "jquants_all_issues_daily_margin"
        )
    return {
        "master": "jquants_master_snapshots",
        "daily_bars": "jquants_daily_bars",
        "market_calendar": "jquants_market_calendar",
        "financial_summaries": "jquants_fin_summaries",
        "margin_alerts": "jquants_margin_alerts",
        "short_sale_reports": "jquants_short_sale_reports",
    }[scope.source]


def import_handoff(
    source: Path, target: Path, request_ids: Sequence[str], *, sources: Sequence[JQuantsSource]
) -> dict[str, object]:
    """Union original pages, then use the existing source projections for L2.

    Calendar ranges are replaced by project_request. Replaying only incoming
    and later overlapping ranges oldest-first retains an incoming old range's
    non-overlap and the newer canonical ranges' overlap. Other sources retain their explicit
    daily full-cross-section replacement contract. No other facts are merged.
    """
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original,
        closing(open_connection(target)) as conn,
    ):
        validate_current_schema(original)
        incoming = [
            scope for scope in _headers(original, set(sources)) if scope.request_id in request_ids
        ]
        if {scope.request_id for scope in incoming} != set(request_ids):
            raise ValueError("J-Quants handoff contains an unexpected or filtered request")
        with conn:
            added = import_requests(original, conn, request_ids)
        complete = []
        incomplete = []
        valid_incoming = []
        for scope in incoming:
            try:
                read_complete_request(conn, scope.request_id)
            except (JQuantsAcquisitionError, ValueError):
                incomplete.append(scope.request_id)
            else:
                valid_incoming.append(scope)
        selected = _related(valid_incoming, _headers(conn, {scope.source for scope in incoming}))
        for scope in selected:
            try:
                request = read_complete_request(conn, scope.request_id)
            except (JQuantsAcquisitionError, ValueError):
                continue
            complete.append((request.received_at, scope.request_id))
    replayed = []
    # Originals were committed above. Projection and coverage for the entire
    # affected replay chain now commit together; failure in newer B cannot leave
    # incoming old A installed. No database copies or unrelated fact merges.
    scopes = {scope.request_id: scope for scope in selected}
    with closing(open_connection(target)) as projection, projection:
        for _, request_id in sorted(complete):
            project_request(target, request_id, connection=projection)
            scope = scopes[request_id]
            if not range_covered(projection, _coverage_source(scope), scope.start, scope.end):
                raise ValueError(
                    "J-Quants projection remains incomplete; retaining canonical facts"
                )
            replayed.append(request_id)
    return {
        "raw_pages_added": added,
        "replayed_requests": replayed,
        "incomplete_requests": incomplete,
    }
