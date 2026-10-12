"""Freeze the saved common-stock universe; never acquire a newer master."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from baibai_engine.market.master_snapshot import MIN_COMMON_STOCK_MASTER_ROWS
from baibai_engine.market.ticker import normalize_ticker
from baibai_engine.market.universe import (
    ELIGIBLE_MARKETS,
    NON_COMMON_STOCK_SECTORS,
    TSE_33_SECTORS,
    UniverseSourceDriftError,
)
from baibai_engine.storage.lake.duck import LakeSession
from baibai_engine.storage.lake.objects import LakeObjectCache
from baibai_engine.storage.lake.reader import (
    FixedRelease,
    LakeReadError,
    accepted_dataset,
    materialize_partitions,
    selected_partitions,
)
from baibai_engine.storage.observations.lake_origin import read_lake_store_origin
from baibai_engine.storage.observations.reader import read_eq_master_asof


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
            "external assessment universe requires a complete exact saved master and L1 origin"
        )
    tickers = _common_stock_tickers(
        [
            (security.ticker, security.market_segment, security.sector_33)
            for security in snapshot.masters
        ]
    )
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
    dataset = accepted_dataset(release, "jquants.master_snapshots")
    manifest = release.dataset_manifest(dataset.name)
    if manifest.data_as_of is not None:
        _require_master_date(manifest.data_as_of)
    partitions = selected_partitions(release, dataset.name, periods=((master_asof.year,),))
    paths = materialize_partitions(cache, dataset=dataset, partitions=partitions)
    if not paths:
        raise ValueError("external assessment universe requires a complete exact saved master")
    try:
        rows = session.connection.execute(
            "SELECT ticker,market,sector_33 FROM read_parquet($files,union_by_name=false) "
            "WHERE snapshot_date=$master_asof ORDER BY ticker",
            {"files": [str(path) for path in paths], "master_asof": master_asof.isoformat()},
        ).fetchall()
    except duckdb.Error as error:
        raise LakeReadError(
            f"external assessment master cannot be read: {session.redact(str(error))}"
        ) from None
    if len(rows) < MIN_COMMON_STOCK_MASTER_ROWS:
        raise ValueError("external assessment universe requires a complete exact saved master")
    tickers = _common_stock_tickers(
        [(str(ticker), str(market or ""), str(sector or "")) for ticker, market, sector in rows]
    )
    return tickers, _binding(tickers, master_asof, release.release_id, release.manifest_sha256)


def calendar_master_asof(
    session: LakeSession,
    *,
    release: FixedRelease,
    cache: LakeObjectCache,
    observation_date: date,
) -> date:
    """Resolve a saved master basis from a continuous, fixed-release JPX calendar."""
    dataset = accepted_dataset(release, "jquants.market_calendar")
    expected = observation_date
    for year in (observation_date.year, observation_date.year - 1):
        partitions = selected_partitions(release, dataset.name, periods=((year,),))
        paths = materialize_partitions(cache, dataset=dataset, partitions=partitions)
        try:
            rows = session.connection.execute(
                "SELECT day,is_business_day FROM read_parquet($files,union_by_name=false) "
                "WHERE day<=$day ORDER BY day DESC",
                {"files": [str(path) for path in paths], "day": expected.isoformat()},
            ).fetchall()
        except duckdb.Error as error:
            raise LakeReadError(
                f"external assessment calendar cannot be read: {session.redact(str(error))}"
            ) from None
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


def _require_master_date(master_asof: date) -> None:
    if master_asof > datetime.now(ZoneInfo("Asia/Tokyo")).date():
        raise ValueError("external assessment saved master cannot be in the future")


def _common_stock_tickers(masters: Sequence[tuple[str, str, str]]) -> tuple[str, ...]:
    selected = []
    for security_code, market, sector in masters:
        if market.upper() not in ELIGIBLE_MARKETS:
            continue
        if sector not in TSE_33_SECTORS | NON_COMMON_STOCK_SECTORS:
            raise UniverseSourceDriftError("Unknown J-Quants sector classification")
        if sector in TSE_33_SECTORS:
            ticker = normalize_ticker(security_code)
            if re.fullmatch(r"[0-9][0-9A-Z]{3}", ticker) is None:
                raise ValueError("invalid TSE common-stock ticker")
            selected.append(ticker)
    tickers = tuple(sorted(selected))
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
