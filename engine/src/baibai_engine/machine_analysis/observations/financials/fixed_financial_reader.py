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
from baibai_engine.machine_analysis.observations.prices.calendar_context import (
    confirmed_closed_master_bases,
)
from baibai_engine.machine_analysis.observations.prices.snapshot_contracts import (
    JQUANTS_MARKET_CALENDAR,
)
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import masters_from_payloads
from baibai_engine.machine_analysis.observations.stocks.snapshot_contracts import (
    JQUANTS_MASTER_SNAPSHOTS,
)
from baibai_engine.storage.snapshots.contracts import LakeDataset
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
            master_manifest = release.dataset_manifest(master_dataset.name)
            native = any(
                source.kind == "native_l1"
                for partition in master_manifest.partitions
                for source in partition.sources
            )
            master_basis = as_of
            if native and JQUANTS_MARKET_CALENDAR.name in release.dataset_names():
                calendar = accepted_dataset(release, JQUANTS_MARKET_CALENDAR)
                calendar_manifest = release.dataset_manifest(calendar.name)
                calendar_parts = selected_partitions(release, calendar.name)
                native_calendar = any(
                    source.kind == "native_l1" and source.namespace == "jquants-market-calendar"
                    for partition in calendar_manifest.partitions
                    for source in partition.sources
                )
                if (
                    native_calendar or calendar_manifest.coverage_status == "complete"
                ) and calendar_manifest.coverage_start <= as_of <= calendar_manifest.data_as_of:
                    # Native calendar rows are individually safe. A distant invalid
                    # day makes the manifest partial, not this contiguous basis.
                    # This release's verified calendar is part of the basis. A
                    # missing local object must not silently select another date.
                    calendar_paths = materialize_partitions(
                        cache, dataset=calendar, partitions=calendar_parts
                    )
                    rows = session.connection.execute(
                        "SELECT day,holiday_division FROM read_parquet(?)",
                        [[str(path) for path in calendar_paths]],
                    ).fetchall()
                    master_basis = confirmed_closed_master_bases(
                        {date.fromisoformat(str(day)): str(division) for day, division in rows},
                        coverage=(
                            (calendar_manifest.coverage_start, calendar_manifest.data_as_of),
                        ),
                    ).get(as_of, as_of)
            # The fixed manifest distinguishes invalid requested dates from
            # ordinary sparse gaps without consulting mutable SQLite or native heads.
            unavailable = max(
                (
                    day.isoformat()
                    for day in master_manifest.unavailable_dates
                    if day <= master_basis
                ),
                default=None,
            )
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
                    "AND (? IS NULL OR snapshot_date > ?) AND (? IS NULL OR security_code = ?)",
                    (
                        master_basis.isoformat(),
                        unavailable,
                        unavailable,
                        ticker,
                        ticker + "0" if ticker is not None else None,
                    ),
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

        def optional_views(
            datasets: tuple[LakeDataset, ...], *, missing_reason: str
        ) -> tuple[bool, str | None]:
            """任意の原本をlocalだけで開く。未配置だけを欠測にし、破損は通常検証で失敗する。"""
            if not all(item.name in release.dataset_names() for item in datasets):
                return False, None
            if not all(
                mirror_path(mirror, obj.key).is_file()
                for item in datasets
                for obj in partition_objects(selected_partitions(release, item.name))
            ):
                return False, missing_reason
            materialized = []
            for item in datasets:
                accepted = accepted_dataset(release, item)
                item_paths = materialize_partitions(
                    cache,
                    dataset=accepted,
                    partitions=selected_partitions(release, item.name),
                )
                if item_paths:
                    session.connection.read_parquet([str(p) for p in item_paths]).create_view(
                        item.sqlite_table
                    )
                materialized.append(bool(item_paths))
            return all(materialized), None

        comparisons: dict[str, tuple[FilingComparisons, ...]] = {}
        comparisons_ready, comparison_input_reason = optional_views(
            (EDINET_DOCUMENTS, EDINET_DOCUMENT_LISTS, EDINET_SOURCE_ARCHIVES),
            missing_reason="fixed_comparison_objects_not_materialized",
        )
        if comparisons_ready:
            comparisons = read_comparison_population(
                session.connection,
                as_of=as_of,
                tickers=(ticker,) if ticker is not None else None,
            )
        pdf_statements: dict[str, tuple[PdfStatement, ...]] = {}
        pdf_ready, pdf_input_reason = optional_views(
            (JPX_SOURCE_BUNDLES, JPX_SOURCE_RESPONSES),
            missing_reason="fixed_pdf_objects_not_materialized",
        )
        if pdf_ready:
            pdf_statements = read_pdf_statements(
                session.connection,
                as_of=as_of,
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
            for code in sorted(
                grouped.keys() | sectors.keys() | comparisons.keys() | pdf_statements.keys()
            )
        }
