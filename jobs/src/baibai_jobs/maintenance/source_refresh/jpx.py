"""Explicit JPX source acquisition followed by replay from the saved bundle."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import requests

from baibai_engine.data_collection.providers.jpx.http import JPXHTTP
from baibai_engine.data_collection.providers.jpx.models import JPXAcquisitionError
from baibai_engine.data_collection.providers.jpx.receipts import (
    JPXBundleError,
    complete_bundle,
    create_bundle,
    declare_bundle_resources,
    retain_bundle_response,
)
from baibai_engine.machine_analysis.observations.disclosures.delistings import (
    JPX_DELISTING_INDEX_URL,
    DelistingRecord,
    resolve_delisting_archive_urls,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_decode import resolve_earnings_calendar_urls
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import (
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
    JPXRegulationSnapshot,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_rebuild import rebuild_jpx_snapshot
from baibai_engine.machine_analysis.observations.disclosures.jpx_sources import (
    JPX_EARNINGS_CALENDAR_INDEX_URL,
    JPX_REGULATION_URLS,
)
from baibai_engine.machine_analysis.observations.disclosures.valuation_catalysts import (
    TSE_CAPITAL_POLICY_URL,
    TSECapitalPolicyRefreshSummary,
)
from baibai_engine.storage.observations.schema import open_connection


class JPXRefresh:
    def __init__(self, *, sqlite_path: Path, session: requests.Session | None = None) -> None:
        self._sqlite_path = sqlite_path
        self._http = JPXHTTP(session)

    def close(self) -> None:
        self._http.close()

    def refresh_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot:
        try:
            with closing(open_connection(self._sqlite_path)) as conn:
                bundle_id = create_bundle(
                    conn,
                    "regulations",
                    asof_date,
                    datetime.now(UTC),
                    tuple(JPX_REGULATION_URLS.items()),
                )
                for name, url in JPX_REGULATION_URLS.items():
                    retain_bundle_response(conn, bundle_id, name, self._http.fetch(url))
                complete_bundle(conn, bundle_id)
            snapshot = rebuild_jpx_snapshot(self._sqlite_path, bundle_id)
            assert isinstance(snapshot, JPXRegulationSnapshot)
            return snapshot
        except (JPXAcquisitionError, JPXBundleError) as exc:
            raise JPXProviderError(str(exc)) from exc

    def refresh_earnings_calendar_snapshot(self, asof_date: date) -> JPXEarningsCalendarSnapshot:
        try:
            with closing(open_connection(self._sqlite_path)) as conn:
                bundle_id = create_bundle(conn, "earnings_calendar", asof_date, datetime.now(UTC))
                index = self._http.fetch(JPX_EARNINGS_CALENDAR_INDEX_URL)
                retain_bundle_response(conn, bundle_id, "index", index)
                urls = resolve_earnings_calendar_urls(
                    index.body, index.requested_url, index.content_type
                )
                declare_bundle_resources(
                    conn,
                    bundle_id,
                    (
                        ("index", index.requested_url),
                        *((f"workbook:{number}", url) for number, url in enumerate(urls)),
                    ),
                )
                for number, url in enumerate(urls):
                    retain_bundle_response(
                        conn, bundle_id, f"workbook:{number}", self._http.fetch(url)
                    )
                complete_bundle(conn, bundle_id)
            snapshot = rebuild_jpx_snapshot(self._sqlite_path, bundle_id)
            assert isinstance(snapshot, JPXEarningsCalendarSnapshot)
            return snapshot
        except (JPXAcquisitionError, JPXBundleError) as exc:
            raise JPXProviderError(str(exc)) from exc

    def refresh_capital_policy_snapshot(self, asof_date: date) -> TSECapitalPolicyRefreshSummary:
        """Capture the workbook first, then rebuild from that completed bundle."""
        try:
            with closing(open_connection(self._sqlite_path)) as conn:
                bundle_id = create_bundle(
                    conn,
                    "capital_policy",
                    asof_date,
                    datetime.now(UTC),
                    (("workbook", TSE_CAPITAL_POLICY_URL),),
                )
                retain_bundle_response(
                    conn, bundle_id, "workbook", self._http.fetch(TSE_CAPITAL_POLICY_URL)
                )
                complete_bundle(conn, bundle_id)
            summary = rebuild_jpx_snapshot(self._sqlite_path, bundle_id)
            assert isinstance(summary, TSECapitalPolicyRefreshSummary)
            return summary
        except (JPXAcquisitionError, JPXBundleError) as exc:
            raise JPXProviderError(str(exc)) from exc

    def refresh_delistings_snapshot(self, asof_date: date) -> tuple[DelistingRecord, ...]:
        """Retain the index and every linked archive before replacing the L2 view."""
        try:
            with closing(open_connection(self._sqlite_path)) as conn:
                bundle_id = create_bundle(conn, "delistings", asof_date, datetime.now(UTC))
                index = self._http.fetch(JPX_DELISTING_INDEX_URL)
                retain_bundle_response(conn, bundle_id, "index", index)
                urls = resolve_delisting_archive_urls(index.body, index.requested_url)
                declare_bundle_resources(
                    conn,
                    bundle_id,
                    (
                        ("index", index.requested_url),
                        *((f"page:{number}", url) for number, url in enumerate(urls)),
                    ),
                )
                for number, url in enumerate(urls):
                    retain_bundle_response(conn, bundle_id, f"page:{number}", self._http.fetch(url))
                complete_bundle(conn, bundle_id)
            snapshot = rebuild_jpx_snapshot(self._sqlite_path, bundle_id)
            assert isinstance(snapshot, tuple)
            return snapshot
        except (JPXAcquisitionError, JPXBundleError) as exc:
            raise JPXProviderError(str(exc)) from exc

    def bootstrap_cache(self, asof_date: date) -> dict[str, int]:
        earnings = self.refresh_earnings_calendar_snapshot(asof_date)
        regulations = self.refresh_regulation_snapshot(asof_date)
        counts = {
            "earnings_calendar_rows": earnings.valid_record_count,
            "regulated_tickers": len(regulations.flags_by_ticker),
        }
        if earnings.superseded_record_count is not None:
            counts["earnings_calendar_superseded"] = earnings.superseded_record_count
        return counts
