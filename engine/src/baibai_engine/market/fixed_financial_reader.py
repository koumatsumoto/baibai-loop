"""既存lakeの固定releaseから、財務文書と業種を読む。作業SQLiteの行は使わない。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.jquants_reader import read_financial_documents
from baibai_engine.market.master_snapshot import masters_from_payloads
from baibai_engine.storage.lake.datasets import JQUANTS_FIN_SUMMARIES, JQUANTS_MASTER_SNAPSHOTS
from baibai_engine.storage.lake.objects import open_lake
from baibai_engine.storage.lake.reader import (
    accepted_dataset,
    materialize_partitions,
    resolve_release,
    selected_partitions,
)
from baibai_engine.storage.observations.lake_origin import LakeStoreOrigin


@dataclass(frozen=True)
class FixedFinancialDocuments:
    documents: tuple[JQuantsFinancialSummary, ...]
    sector: str | None


def read_fixed_financial_documents(
    mirror: Path, *, origin: LakeStoreOrigin, ticker: str, as_of: date
) -> FixedFinancialDocuments:
    """Read only verified local objects; never fetch sources, R2, or mutable SQLite rows."""
    with open_lake(mirror=mirror) as (session, cache):
        release = resolve_release(
            cache.source, origin.release_id,
            manifest_sha256=origin.release_manifest_sha256,
        )
        dataset = accepted_dataset(release, JQUANTS_FIN_SUMMARIES.name)
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
        sector = None
        if JQUANTS_MASTER_SNAPSHOTS.name in release.dataset_names():
            master_dataset = accepted_dataset(release, JQUANTS_MASTER_SNAPSHOTS.name)
            master_paths = materialize_partitions(
                cache, dataset=master_dataset,
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
                    "AND security_code = ?",
                    (as_of.isoformat(), ticker + "0"),
                ).fetchall()
                securities = masters_from_payloads([str(row[0]) for row in rows])
                sector = securities[0].sector_33 if securities else None
        return FixedFinancialDocuments(tuple(documents), sector)
