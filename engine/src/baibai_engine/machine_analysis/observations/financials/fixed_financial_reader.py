"""既存lakeの固定releaseから、財務文書と業種を読む。作業SQLiteの行は使わない。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from baibai_engine.data_collection.providers.edinet.snapshot_contracts import (
    EDINET_SOURCE_ARCHIVES,
)
from baibai_engine.data_collection.providers.jpx.snapshot_contracts import (
    JPX_SOURCE_BUNDLES,
    JPX_SOURCE_RESPONSES,
)
from baibai_engine.machine_analysis.observations.disclosures.snapshot_contracts import (
    EDINET_DOCUMENT_LISTS,
    EDINET_DOCUMENTS,
)
from baibai_engine.machine_analysis.observations.financials.edinet_comparison_reader import (
    read_comparison_population,
)
from baibai_engine.machine_analysis.observations.financials.edinet_comparisons import (
    FilingComparisons,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statement_reader import (
    read_pdf_statements,
)
from baibai_engine.machine_analysis.observations.financials.pdf_statements import PdfStatement
from baibai_engine.machine_analysis.observations.financials.reader import (
    read_financial_documents,
)
from baibai_engine.machine_analysis.observations.financials.snapshot_contracts import (
    JQUANTS_FIN_SUMMARIES,
)
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import masters_from_payloads
from baibai_engine.machine_analysis.observations.stocks.snapshot_contracts import (
    JQUANTS_MASTER_SNAPSHOTS,
)
from baibai_engine.storage.snapshots.objects import mirror_path, open_lake
from baibai_engine.storage.snapshots.reader import (
    accepted_dataset,
    materialize_partitions,
    partition_objects,
    resolve_release,
    selected_partitions,
)
from baibai_engine.storage.sqlite.source_cache.lake_origin import LakeStoreOrigin


@dataclass(frozen=True)
class FixedFinancialDocuments:
    documents: tuple[JQuantsFinancialSummary, ...]
    sector: str | None
    comparisons: tuple[FilingComparisons, ...] = ()
    comparison_input_reason: str | None = None
    pdf_statements: tuple[PdfStatement, ...] = ()
    pdf_input_reason: str | None = None


def read_fixed_financial_documents(
    mirror: Path, *, origin: LakeStoreOrigin, ticker: str, as_of: date
) -> FixedFinancialDocuments:
    """Read only verified local objects; never fetch sources, R2, or mutable SQLite rows."""
    return _read_fixed_financial_population(mirror, origin=origin, ticker=ticker, as_of=as_of).get(
        ticker, FixedFinancialDocuments((), None)
    )


def read_fixed_financial_population(
    mirror: Path, *, origin: LakeStoreOrigin, as_of: date
) -> dict[str, FixedFinancialDocuments]:
    """全社生成でreleaseとparquetを一度だけ開く。個社queryの反復はしない。"""
    return _read_fixed_financial_population(mirror, origin=origin, ticker=None, as_of=as_of)


def _read_fixed_financial_population(
    mirror: Path, *, origin: LakeStoreOrigin, ticker: str | None, as_of: date
) -> dict[str, FixedFinancialDocuments]:
    with open_lake(mirror=mirror) as (session, cache):
        release = resolve_release(
            cache.source,
            origin.release_id,
            manifest_sha256=origin.release_manifest_sha256,
        )
        dataset = accepted_dataset(release, JQUANTS_FIN_SUMMARIES)
        paths = materialize_partitions(
            cache, dataset=dataset, partitions=selected_partitions(release, dataset.name)
        )
        documents = []
        if paths:
            session.connection.read_parquet([str(path) for path in paths]).create_view(
                "jquants_fin_summaries"
            )
            documents = read_financial_documents(
                session.connection, start=date.min, end=date.max, ticker=ticker
            )
        sectors: dict[str, str] = {}
        if JQUANTS_MASTER_SNAPSHOTS.name in release.dataset_names():
            master_dataset = accepted_dataset(release, JQUANTS_MASTER_SNAPSHOTS)
            master_paths = materialize_partitions(
                cache,
                dataset=master_dataset,
                partitions=selected_partitions(release, master_dataset.name),
            )
            if master_paths:
                session.connection.read_parquet([str(p) for p in master_paths]).create_view(
                    "jquants_master_snapshots"
                )
                rows = session.connection.execute(
                    "SELECT record_json FROM jquants_master_snapshots "
                    "WHERE snapshot_date = (SELECT MAX(snapshot_date) "
                    "FROM jquants_master_snapshots WHERE snapshot_date <= ?) "
                    "AND (? IS NULL OR security_code = ?)",
                    (as_of.isoformat(), ticker, ticker + "0" if ticker is not None else None),
                ).fetchall()
                securities = masters_from_payloads([str(row[0]) for row in rows])
                sectors = {
                    security.ticker: security.sector_33
                    for security in securities
                    if ticker is not None or security.is_common_stock
                }
        grouped: dict[str, list[JQuantsFinancialSummary]] = {}
        for row in documents:
            grouped.setdefault(row.ticker, []).append(row)
        comparisons: dict[str, tuple[FilingComparisons, ...]] = {}
        comparison_input_reason = None
        required = (EDINET_DOCUMENTS, EDINET_DOCUMENT_LISTS, EDINET_SOURCE_ARCHIVES)
        published = all(item.name in release.dataset_names() for item in required)
        # This reader is explicitly local-only. Missing optional native objects do
        # not invalidate the independently verified J-Quants observations. Present
        # objects still pass the normal digest/schema checks and fail on corruption.
        present = published and all(
            mirror_path(mirror, obj.key).is_file()
            for item in required
            for obj in partition_objects(selected_partitions(release, item.name))
        )
        if published and not present:
            comparison_input_reason = "fixed_comparison_objects_not_materialized"
        if present:
            materialized = []
            for item in required:
                accepted = accepted_dataset(release, item)
                item_paths = materialize_partitions(
                    cache,
                    dataset=accepted,
                    partitions=selected_partitions(release, accepted.name),
                )
                if item_paths:
                    session.connection.read_parquet([str(p) for p in item_paths]).create_view(
                        item.sqlite_table
                    )
                materialized.append(bool(item_paths))
            if all(materialized):
                comparisons = read_comparison_population(
                    session.connection,
                    as_of=as_of,
                    tickers=(ticker,) if ticker is not None else None,
                )
        pdf_statements: dict[str, tuple[PdfStatement, ...]] = {}
        pdf_input_reason = None
        pdf_datasets = (JPX_SOURCE_BUNDLES, JPX_SOURCE_RESPONSES)
        pdf_published = all(item.name in release.dataset_names() for item in pdf_datasets)
        pdf_present = pdf_published and all(
            mirror_path(mirror, obj.key).is_file()
            for item in pdf_datasets
            for obj in partition_objects(selected_partitions(release, item.name))
        )
        if pdf_published and not pdf_present:
            pdf_input_reason = "fixed_pdf_objects_not_materialized"
        if pdf_present:
            materialized = []
            for item in pdf_datasets:
                accepted = accepted_dataset(release, item)
                item_paths = materialize_partitions(
                    cache, dataset=accepted, partitions=selected_partitions(release, item.name)
                )
                if item_paths:
                    session.connection.read_parquet([str(p) for p in item_paths]).create_view(
                        item.sqlite_table
                    )
                materialized.append(bool(item_paths))
            if all(materialized):
                pdf_statements = read_pdf_statements(
                    session.connection, as_of=as_of,
                    tickers=(ticker,) if ticker is not None else None,
                )
        return {
            code: FixedFinancialDocuments(
                tuple(grouped.get(code, ())),
                sectors.get(code),
                comparisons.get(code, ()),
                comparison_input_reason,
                pdf_statements.get(code, ()),
                pdf_input_reason,
            )
            for code in sorted(grouped.keys() | sectors.keys() | comparisons.keys() | pdf_statements.keys())
        }
