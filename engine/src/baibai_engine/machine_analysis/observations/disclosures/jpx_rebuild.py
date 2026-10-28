"""Rebuild JPX L2 snapshots from one explicitly selected complete source bundle."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from baibai_engine.data_collection.providers.jpx.receipts import JPXSourceBundle, read_complete_bundle
from baibai_engine.machine_analysis.observations.disclosures.delistings import (
    DelistingRecord,
    DelistingSourceError,
    build_delisting_snapshot,
    resolve_delisting_archive_urls,
    store_jpx_delistings,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_decode import (
    build_earnings_calendar_snapshot,
    build_regulation_snapshot,
    parse_earnings_calendar_excel,
    parse_regulation_rows,
    resolve_earnings_calendar_urls,
    validate_allowed_url,
    validate_earnings_calendar_url,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import (
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
    JPXRegulationSnapshot,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_sources import JPX_REGULATION_URLS
from baibai_engine.machine_analysis.observations.normalization import normalize_ticker_or_none, to_str_or_none
from baibai_engine.machine_analysis.observations.disclosures.valuation_catalysts import (
    TSECapitalPolicyRefreshSummary,
    parse_tse_capital_policy_workbook,
    store_tse_capital_policy_rows,
)
from baibai_engine.storage.observations.coverage import (
    date_range_row_count,
    delete_overlapping_source_coverage,
    delete_source_coverage,
    record_source_coverage,
)
from baibai_engine.storage.observations.schema import connect_current, open_connection

_LOGGER = logging.getLogger(__name__)

JPXSnapshot = (
    JPXRegulationSnapshot
    | JPXEarningsCalendarSnapshot
    | TSECapitalPolicyRefreshSummary
    | tuple[DelistingRecord, ...]
)


def rebuild_jpx_snapshot(sqlite_path: Path, bundle_id: str) -> JPXSnapshot:
    """Use only this bundle's retained bytes, then replace its validated L2 view."""
    conn = connect_current(sqlite_path)
    if conn is None:
        raise JPXProviderError(f"JPX source store is unavailable: {sqlite_path}")
    try:
        bundle = read_complete_bundle(conn, bundle_id)
    finally:
        conn.close()
    if bundle.collection_name == "regulations":
        regulations_snapshot = _regulations(bundle)
        assert bundle.completed_at is not None
        store_jpx_regulations(
            sqlite_path,
            bundle.asof_date,
            flags_by_ticker=regulations_snapshot.flags_by_ticker,
            source_names=regulations_snapshot.source_names,
            fetched_at_utc=bundle.completed_at.isoformat(timespec="microseconds"),
        )
        return regulations_snapshot
    if bundle.collection_name == "earnings_calendar":
        earnings_snapshot = _earnings_calendar(bundle)
        assert bundle.completed_at is not None
        store_jpx_earnings_calendar_snapshot(
            sqlite_path,
            earnings_snapshot,
            fetched_at_utc=bundle.completed_at.isoformat(timespec="microseconds"),
        )
        return earnings_snapshot
    if bundle.collection_name == "capital_policy":
        return _capital_policy(bundle, sqlite_path)
    if bundle.collection_name == "delistings":
        return _delistings(bundle, sqlite_path)
    raise JPXProviderError(f"JPX bundle collection is not rebuildable: {bundle.collection_name}")


def _regulations(bundle: JPXSourceBundle) -> JPXRegulationSnapshot:
    resources = {item.resource_name: item.response for item in bundle.responses}
    if set(resources) != set(JPX_REGULATION_URLS):
        raise JPXProviderError("JPX regulation bundle must contain the four required sources")
    parsed_sources: dict[str, list[dict[str, str]]] = {}
    for name in JPX_REGULATION_URLS:
        response = resources[name]
        validate_allowed_url(response.requested_url)
        suffix = Path(urlparse(response.requested_url).path).suffix.lower()
        content_type = response.content_type.split(";", 1)[0].strip().lower()
        if suffix != ".html" and content_type != "text/html":
            raise JPXProviderError(
                f"unsupported JPX regulation source format: {response.requested_url}"
            )
        parsed_sources[name] = parse_regulation_rows(
            name, response.body, response.requested_url, response.content_type
        )
    return build_regulation_snapshot(parsed_sources)


def _earnings_calendar(bundle: JPXSourceBundle) -> JPXEarningsCalendarSnapshot:
    receipts = {item.resource_name: item.response for item in bundle.responses}
    index = receipts.get("index")
    if index is None:
        raise JPXProviderError("JPX earnings bundle has no index response")
    source_urls = resolve_earnings_calendar_urls(
        index.body, index.requested_url, index.content_type
    )
    expected = (
        ("index", index.requested_url),
        *((f"workbook:{number}", url) for number, url in enumerate(source_urls)),
    )
    if bundle.expected_resources != expected:
        raise JPXProviderError("JPX earnings bundle manifest differs from retained index links")
    files = []
    for number, url in enumerate(source_urls):
        validate_earnings_calendar_url(url)
        files.append(parse_earnings_calendar_excel(receipts[f"workbook:{number}"].body, url))
    return build_earnings_calendar_snapshot(
        files, asof_date=bundle.asof_date, source_urls=source_urls
    )


def _capital_policy(bundle: JPXSourceBundle, sqlite_path: Path) -> TSECapitalPolicyRefreshSummary:
    if len(bundle.responses) != 1 or bundle.responses[0].resource_name != "workbook":
        raise JPXProviderError("JPX capital-policy bundle requires one workbook")
    receipt = bundle.responses[0].response
    if bundle.expected_resources != (("workbook", receipt.requested_url),):
        raise JPXProviderError("JPX capital-policy manifest must declare one workbook")
    validate_allowed_url(receipt.requested_url)
    rows = parse_tse_capital_policy_workbook(receipt.body)
    summary = TSECapitalPolicyRefreshSummary(
        sheet_count=len({row.snapshot_month_end for row in rows}),
        row_count=len(rows),
        disclosed_count=sum(row.status == "disclosed" for row in rows),
        considering_count=sum(row.status == "considering" for row in rows),
    )
    store_tse_capital_policy_rows(sqlite_path, rows)
    return summary


def _delistings(bundle: JPXSourceBundle, sqlite_path: Path) -> tuple[DelistingRecord, ...]:
    receipts = {item.resource_name: item.response for item in bundle.responses}
    index = receipts.get("index")
    if index is None:
        raise DelistingSourceError("JPX delisting bundle has no index")
    archive_urls = resolve_delisting_archive_urls(index.body, index.requested_url)
    expected = (
        ("index", index.requested_url),
        *((f"page:{number}", url) for number, url in enumerate(archive_urls)),
    )
    if bundle.expected_resources != expected:
        raise DelistingSourceError("JPX delisting manifest differs from retained index links")
    pages = (
        (index.requested_url, index.body),
        *((url, receipts[f"page:{number}"].body) for number, url in enumerate(archive_urls)),
    )
    rows = build_delisting_snapshot(pages)
    store_jpx_delistings(sqlite_path, rows)
    return rows


def store_jpx_earnings_calendar_snapshot(
    db_path: Path,
    snapshot: JPXEarningsCalendarSnapshot,
    *,
    fetched_at_utc: str | None = None,
) -> int:
    """Replace the current JPX earnings-calendar snapshot."""
    if not snapshot.entries:
        raise ValueError("JPX earnings calendar snapshot must contain at least one valid row")
    conn = open_connection(db_path)
    fetched_at = fetched_at_utc or datetime.now(UTC).isoformat()
    try:
        conn.execute("DELETE FROM jpx_earnings_calendar")
        delete_source_coverage(conn, "jpx_earnings_calendar")
        rows = [(entry.announcement_date.isoformat(), entry.ticker) for entry in snapshot.entries]
        conn.executemany(
            "INSERT INTO jpx_earnings_calendar(announcement_date, ticker) VALUES (?, ?)",
            rows,
        )
        persisted_count = int(
            conn.execute("SELECT COUNT(*) FROM jpx_earnings_calendar").fetchone()[0]
        )
        record_source_coverage(
            conn,
            source="jpx_earnings_calendar",
            coverage_key="get_earnings_calendar_snapshot:current",
            coverage_start=snapshot.min_date.isoformat(),
            coverage_end=snapshot.max_date.isoformat(),
            fetched_at_utc=fetched_at,
            record_count=persisted_count,
            status="ok",
            error=None,
        )
        conn.commit()
        _LOGGER.info(
            "stored JPX earnings calendar snapshot: sources=%s raw=%d valid=%d "
            "excluded=%d superseded=%s min=%s max=%s",
            ",".join(snapshot.source_urls),
            snapshot.raw_record_count,
            snapshot.valid_record_count,
            snapshot.excluded_record_count,
            snapshot.superseded_record_count,
            snapshot.min_date.isoformat(),
            snapshot.max_date.isoformat(),
        )
        return persisted_count
    finally:
        conn.close()


def store_jpx_regulations(
    db_path: Path,
    asof_date: date,
    *,
    flags_by_ticker: Mapping[str, Iterable[str]],
    source_names: Iterable[str],
    fetched_at_utc: str | None = None,
) -> int:
    conn = open_connection(db_path)
    fetched_at = fetched_at_utc or datetime.now(UTC).isoformat()
    source_name_tuple = tuple(source_names)
    try:
        conn.execute(
            "DELETE FROM jpx_regulation_flags WHERE asof_date = ?", (asof_date.isoformat(),)
        )
        conn.execute(
            "DELETE FROM jpx_regulation_sources WHERE asof_date = ?",
            (asof_date.isoformat(),),
        )
        delete_overlapping_source_coverage(conn, "jpx_regulation_flags", asof_date, asof_date)
        source_rows = [(asof_date.isoformat(), str(name), fetched_at) for name in source_name_tuple]
        if source_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO jpx_regulation_sources("
                "asof_date, source_name, fetched_at_utc"
                ") VALUES (?, ?, ?)",
                source_rows,
            )
        rows: list[tuple[Any, ...]] = []
        rejected_count = 0
        for raw_ticker, flags in flags_by_ticker.items():
            ticker = normalize_ticker_or_none(raw_ticker)
            if ticker is None:
                rejected_count += 1
                continue
            accepted_for_ticker = 0
            rejected_for_ticker = 0
            for flag in flags:
                flag_text = to_str_or_none(flag)
                if flag_text is None:
                    rejected_for_ticker += 1
                    continue
                rows.append((asof_date.isoformat(), flag_text, ticker, flag_text, fetched_at))
                accepted_for_ticker += 1
            rejected_count += rejected_for_ticker
            if accepted_for_ticker == 0 and rejected_for_ticker == 0:
                rejected_count += 1
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO jpx_regulation_flags("
                "asof_date, source_name, ticker, flag, fetched_at_utc"
                ") VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        persisted_count = date_range_row_count(
            conn, "jpx_regulation_flags", "asof_date", asof_date, asof_date
        )
        record_source_coverage(
            conn,
            source="jpx_regulation_flags",
            coverage_key=asof_date.isoformat(),
            coverage_start=asof_date.isoformat(),
            coverage_end=asof_date.isoformat(),
            fetched_at_utc=fetched_at,
            record_count=persisted_count,
            status="partial" if rejected_count else "ok",
            error=(
                f"{rejected_count} JPX regulation records were rejected" if rejected_count else None
            ),
        )
        conn.commit()
        return persisted_count
    finally:
        conn.close()
