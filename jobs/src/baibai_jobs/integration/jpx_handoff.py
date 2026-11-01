"""JPX original bundles with collection-specific replacement scopes."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from baibai_engine.data_collection.providers.jpx.receipts import (
    JPXBundleError,
    read_complete_bundle,
)
from baibai_engine.data_collection.receipts.merge import copy_originals
from baibai_engine.machine_analysis.observations.disclosures.capital_policy import (
    CapitalPolicyError,
)
from baibai_engine.machine_analysis.observations.disclosures.delistings import DelistingSourceError
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import JPXProviderError
from baibai_engine.machine_analysis.observations.disclosures.jpx_rebuild import rebuild_jpx_snapshot
from baibai_engine.machine_analysis.observations.financials.pdf_statements import extract_pdf
from baibai_engine.storage.sqlite.source_cache.schema import (
    open_connection,
    validate_current_schema,
)
from baibai_jobs.integration.replay_result import ReplayResult

# These three owners replace their entire current view. Regulations replace only
# one as-of day. Keep these contracts explicit rather than using a provider LWW.
_GLOBAL_COLLECTIONS = {"earnings_calendar", "capital_policy", "delistings"}
_COLLECTIONS = _GLOBAL_COLLECTIONS | {"regulations", "financial_statements"}


def export_handoff(
    source: Path, output: Path, *, since: datetime, collection: str
) -> tuple[str, ...]:
    if output.exists():
        raise FileExistsError(output)
    if collection not in _COLLECTIONS or since.tzinfo is None:
        raise ValueError("invalid JPX handoff selection")
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as raw,
        closing(open_connection(output)) as target,
        target,
    ):
        validate_current_schema(raw)
        raw.execute("BEGIN")
        bundles = tuple(
            row[0]
            for row in raw.execute(
                "SELECT bundle_id FROM jpx_source_bundles "
                "WHERE collection_name = ? AND started_at_utc >= ? "
                "ORDER BY started_at_utc, bundle_id",
                (collection, since.astimezone(UTC).isoformat(timespec="microseconds")),
            )
        )
        for table in ("jpx_source_bundles", "jpx_source_responses"):
            copy_originals(raw, target, table, [(key,) for key in bundles], selection="bundle_id")
    return bundles


def import_handoff(
    source: Path, target: Path, bundle_ids: tuple[str, ...], *, collection: str
) -> ReplayResult:
    if collection not in _COLLECTIONS:
        raise ValueError("invalid JPX handoff collection")
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as raw,
        closing(open_connection(target)) as conn,
    ):
        validate_current_schema(raw)
        scopes = set()
        for bundle_id in bundle_ids:
            row = raw.execute(
                "SELECT collection_name, asof_date FROM jpx_source_bundles WHERE bundle_id = ?",
                (bundle_id,),
            ).fetchone()
            if row is None or row[0] != collection:
                raise ValueError("JPX handoff differs from the selected collection")
            scopes.add(row[1] if collection == "regulations" else "current")
        with conn:
            added = sum(
                copy_originals(
                    raw, conn, table, [(key,) for key in bundle_ids], selection="bundle_id"
                )
                for table in ("jpx_source_bundles", "jpx_source_responses")
            )
        if collection == "financial_statements":
            # Additive originals, never provider-wide latest-wins or replacement.
            # L2 is deterministic on fixed read; report unsupported inputs here too.
            failures = []
            for bundle_id in bundle_ids:
                try:
                    bundle = read_complete_bundle(conn, bundle_id)
                    for receipt in bundle.responses:
                        response = receipt.response
                        if (
                            response.content_type.split(";", 1)[0].strip().lower()
                            != "application/pdf"
                        ):
                            raise ValueError("financial PDF media type differs from its collection")
                        value = extract_pdf(
                            content=response.body,
                            source_url=response.requested_url,
                            received_at=response.received_at,
                            bundle_id=bundle_id,
                            resource_name=receipt.resource_name,
                        )
                        if receipt.resource_name != "pdf:" + value.disclosure_number:
                            raise ValueError("financial PDF resource differs from disclosure")
                        if value.reason:
                            failures.append({"bundle_id": bundle_id, "reason": value.reason})
                except (JPXBundleError, ValueError) as error:
                    failures.append({"bundle_id": bundle_id, "reason": str(error)})
            return ReplayResult(
                report={
                    "raw_rows_added": added,
                    "retained_bundles": list(bundle_ids),
                    "projection_failures": failures,
                },
                complete=not failures,
            )
        selected = []
        incomplete = []
        for scope in scopes:
            candidates = conn.execute(
                "SELECT bundle_id FROM jpx_source_bundles WHERE collection_name = ? "
                "AND (? = 'current' OR asof_date = ?) AND completed_at_utc IS NOT NULL "
                "ORDER BY completed_at_utc DESC, bundle_id DESC LIMIT 1",
                (collection, scope, scope),
            ).fetchone()
            if candidates:
                selected.append(candidates[0])
        for bundle_id in bundle_ids:
            try:
                read_complete_bundle(conn, bundle_id)
            except JPXBundleError:
                incomplete.append(bundle_id)
    replayed, failed = [], []
    for bundle_id in selected:
        try:
            rebuild_jpx_snapshot(target, bundle_id)
        except (
            JPXProviderError,
            DelistingSourceError,
            CapitalPolicyError,
            JPXBundleError,
            ValueError,
        ):
            failed.append(bundle_id)
        else:
            replayed.append(bundle_id)
    return ReplayResult(
        report={
            "raw_rows_added": added,
            "replayed_bundles": replayed,
            "incomplete_bundles": incomplete,
            "projection_failures": failed,
        },
        complete=not (incomplete or failed),
    )
