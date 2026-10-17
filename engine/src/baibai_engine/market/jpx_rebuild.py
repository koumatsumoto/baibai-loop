"""Rebuild JPX L2 snapshots from one explicitly selected complete source bundle."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from baibai_engine.collection.jpx.receipts import JPXSourceBundle, read_complete_bundle
from baibai_engine.market.jpx_decode import (
    build_earnings_calendar_snapshot,
    build_regulation_snapshot,
    parse_earnings_calendar_excel,
    parse_regulation_rows,
    resolve_earnings_calendar_urls,
    validate_allowed_url,
    validate_earnings_calendar_url,
)
from baibai_engine.market.jpx_models import (
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
    JPXRegulationSnapshot,
)
from baibai_engine.market.jpx_sources import JPX_REGULATION_URLS
from baibai_engine.market.valuation_catalysts import (
    TSECapitalPolicyRefreshSummary,
    parse_tse_capital_policy_workbook,
    store_tse_capital_policy_rows,
)
from baibai_engine.storage.observations.ingest.jpx import (
    store_jpx_earnings_calendar_snapshot,
    store_jpx_regulations,
)
from baibai_engine.storage.observations.schema import connect_current

JPXSnapshot = JPXRegulationSnapshot | JPXEarningsCalendarSnapshot | TSECapitalPolicyRefreshSummary


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
