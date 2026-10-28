"""Freeze the saved common-stock universe; never acquire a newer master."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from baibai_engine.data_collection.providers.jquants.dependencies import read_dependency
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import (
    MIN_COMMON_STOCK_MASTER_ROWS,
    masters_from_payloads,
    validate_master_snapshot,
)
from baibai_engine.machine_analysis.observations.stocks.models import SecurityMaster
from baibai_engine.machine_analysis.observations.stocks.reader import (
    read_eq_master_asof,
)
from baibai_engine.machine_analysis.observations.stocks.universe import ELIGIBLE_MARKETS
from baibai_engine.storage.snapshots.duck import LakeSession
from baibai_engine.storage.snapshots.objects import LakeObjectCache
from baibai_engine.storage.snapshots.reader import (
    FixedRelease,
    LakeReadError,
    accepted_dataset,
    materialize_partitions,
    selected_partitions,
)
from baibai_engine.storage.sqlite.source_cache.lake_origin import read_lake_store_origin
from baibai_engine.machine_analysis.observations.stocks.snapshot_contracts import (
    JQUANTS_MASTER_SNAPSHOTS,
)
from baibai_engine.machine_analysis.observations.prices.snapshot_contracts import (
    JQUANTS_MARKET_CALENDAR,
)


def frozen_universe(database: Path, master_asof: date) -> tuple[tuple[str, ...], str]:
    """Require an exact saved master and a hydrated fixed-release origin."""
    _require_master_date(master_asof)
    snapshot = read_eq_master_asof(database, master_asof)
    origin = read_lake_store_origin(database)
    if (
        snapshot.status != "exact_date"
        or len(snapshot.masters) < MIN_COMMON_STOCK_MASTER_ROWS
        or origin is None
    ):
        raise ValueError(
            "external assessment universe requires a complete exact saved master "
            "and fixed lake origin"
        )
    tickers = _common_stock_tickers(snapshot.masters)
    return tickers, _binding(
        tickers, master_asof, origin.release_id, origin.release_manifest_sha256
    )


def frozen_lake_universe(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    master_asof: date,
) -> tuple[tuple[str, ...], str]:
    """Read only the digest-pinned master year, with no SQLite hydrate or fallback."""
    _require_master_date(master_asof)
    dataset = accepted_dataset(release, JQUANTS_MASTER_SNAPSHOTS)
    manifest = release.dataset_manifest(dataset.name)
    if manifest.data_as_of is not None:
        _require_master_date(manifest.data_as_of)
        if manifest.data_as_of < master_asof:
            raise ValueError("external assessment universe requires a complete exact saved master")
    partitions = selected_partitions(release, dataset.name, periods=((master_asof.year,),))
    paths = materialize_partitions(cache, dataset=dataset, partitions=partitions)
    if not paths:
        raise ValueError("external assessment universe requires a complete exact saved master")
    try:
        rows = session.connection.execute(
            "SELECT record_json FROM read_parquet($files,union_by_name=false) "
            "WHERE snapshot_date=$master_asof ORDER BY security_code",
            {"files": [str(path) for path in paths], "master_asof": master_asof.isoformat()},
        ).fetchall()
    except duckdb.Error as error:
        raise LakeReadError(
            f"external assessment master cannot be read: {session.redact(str(error))}"
        ) from None
    if len(rows) < MIN_COMMON_STOCK_MASTER_ROWS:
        raise ValueError("external assessment universe requires a complete exact saved master")
    tickers = _common_stock_tickers(masters_from_payloads([row[0] for row in rows]))
    return tickers, _binding(tickers, master_asof, release.release_id, release.manifest_sha256)


def calendar_master_asof(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    observation_date: date,
    overrides: Mapping[date, bool] | None = None,
) -> date:
    """Resolve a saved master basis from a continuous, fixed-release JPX calendar."""
    dataset = accepted_dataset(release, JQUANTS_MARKET_CALENDAR)
    expected = observation_date
    for year in (observation_date.year, observation_date.year - 1):
        partitions = selected_partitions(release, dataset.name, periods=((year,),))
        paths = materialize_partitions(cache, dataset=dataset, partitions=partitions)
        try:
            rows = (
                session.connection.execute(
                    "SELECT day,holiday_division IN ('1', '2') "
                    "FROM read_parquet($files,union_by_name=false) "
                    "WHERE day<=$day ORDER BY day DESC",
                    {"files": [str(path) for path in paths], "day": expected.isoformat()},
                ).fetchall()
                if paths
                else []
            )
        except duckdb.Error as error:
            raise LakeReadError(
                f"external assessment calendar cannot be read: {session.redact(str(error))}"
            ) from None
        if overrides:
            merged = {date.fromisoformat(str(day)): business for day, business in rows}
            merged.update(
                {
                    day: int(business)
                    for day, business in overrides.items()
                    if day.year == year and day <= expected
                }
            )
            rows = sorted(merged.items(), reverse=True)
        for day, business in rows:
            if str(day) != expected.isoformat() or business not in (0, 1):
                raise ValueError(
                    "external assessment continuous exact-date JPX calendar is unavailable"
                )
            if business == 1:
                return expected
            expected -= timedelta(days=1)
        if expected.year == year:
            raise ValueError(
                "external assessment continuous exact-date JPX calendar is unavailable"
            )
    raise ValueError("external assessment previous business day is unavailable in JPX calendar")


def received_master_universe(value: dict[str, Any], master_asof: date) -> tuple[str, ...]:
    """Use an exact complete native receipt without assigning a full lake origin."""
    _require_master_date(master_asof)
    request = read_dependency(value)
    if request.source != "master" or request.params != {"date": master_asof.isoformat()}:
        raise ValueError("external assessment requires an exact-date master receipt")
    snapshot = validate_master_snapshot(request.records(), master_asof)
    return _common_stock_tickers(masters_from_payloads([row[2] for row in snapshot.rows]))


def received_calendar(value: dict[str, Any]) -> dict[date, bool]:
    request = read_dependency(value)
    if request.source != "market_calendar":
        raise ValueError("external assessment requires a calendar receipt")
    first, last = (
        date.fromisoformat(request.params["from"]),
        date.fromisoformat(request.params["to"]),
    )
    rows = request.records()
    result = {date.fromisoformat(row["Date"]): str(row["HolDiv"]) in {"1", "2"} for row in rows}
    if (
        last < first
        or len(result) != len(rows)
        or len(result) != (last - first).days + 1
        or any(day < first or day > last for day in result)
    ):
        raise ValueError("external assessment dependency calendar is incomplete")
    return result


def _require_master_date(master_asof: date) -> None:
    if master_asof > datetime.now(ZoneInfo("Asia/Tokyo")).date():
        raise ValueError("external assessment saved master cannot be in the future")


def _common_stock_tickers(masters: Sequence[SecurityMaster]) -> tuple[str, ...]:
    tickers = tuple(
        sorted(
            security.ticker
            for security in masters
            if security.is_common_stock and security.market_segment.upper() in ELIGIBLE_MARKETS
        )
    )
    if any(re.fullmatch(r"[0-9][0-9A-Z]{3}", ticker) is None for ticker in tickers):
        raise ValueError("invalid TSE common-stock ticker")
    if len(tickers) < MIN_COMMON_STOCK_MASTER_ROWS or len(set(tickers)) != len(tickers):
        raise ValueError(
            "external assessment universe requires a complete distinct common-stock population"
        )
    return tickers


def _binding(
    tickers: tuple[str, ...], master_asof: date, release_id: str, manifest_sha256: str
) -> str:
    return json.dumps(
        {
            "master_asof": master_asof.isoformat(),
            "release_id": release_id,
            "release_manifest_sha256": manifest_sha256,
            "tickers_sha256": hashlib.sha256("\n".join(tickers).encode()).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
