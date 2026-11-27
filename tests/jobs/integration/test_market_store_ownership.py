"""Every Market SQLite table is readable in Lake or local to its owner."""

from baibai_engine.storage.sqlite.source_cache.schema import transfer_schema_shape
from baibai_jobs.publication.datasets import LAKE_DATASETS


def test_market_tables_have_one_storage_owner() -> None:
    # The readable Lake registry includes historical contracts as well as daily ones.
    _, columns_by_table = transfer_schema_shape()
    tables = {name for name in columns_by_table if not name.startswith("sqlite_")}
    local = {"source_coverage", "lake_store_origin", "edinet_facts_scope"}
    lake = {dataset.sqlite_table for dataset in LAKE_DATASETS.values()}

    assert local | lake == tables
    assert local.isdisjoint(lake)
