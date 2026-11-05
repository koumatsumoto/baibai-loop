"""Select annual XBRL originals from source scope and native document evidence."""

from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.edinet.facts_scope import (
    FactsScope,
    merge_scope,
    read_scope,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import (
    read_stored_documents,
)
from baibai_engine.machine_analysis.observations.financials.edinet_decode import parse_sec_code
from baibai_engine.machine_analysis.observations.financials.edinet_facts.models import (
    filing_identity,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.selection import (
    annual_documents,
    usable_document,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def prepare_fact_acquisition(
    database: Path, *, asof: date, initialize: bool = False, tickers: tuple[str, ...] = ()
) -> list[dict[str, Any]] | None:
    with closing(open_connection(database)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        scope = read_scope(connection)
        if scope.since is None and not initialize:
            return None
        selected = annual_documents(
            read_stored_documents(connection, through=asof),
            retained=set(scope.doc_ids),
            since=scope.since.isoformat() if scope.since else None,
        )
        if tickers:
            selected = [doc for doc in selected if parse_sec_code(doc["secCode"]) in tickers]
        # Keep selected but presently unavailable families before any HTTP/L2 work.
        merge_scope(
            connection, FactsScope(doc_ids=frozenset(str(doc["docID"]) for doc in selected))
        )
    return selected


def needs_archive(document: dict[str, Any], *, asof: date) -> bool:
    try:
        identity = filing_identity(
            parse_sec_code(document["secCode"]),
            str(document["docID"]),
            str(document.get("submitDateTime") or ""),
        )
    except ValueError:
        return False
    return date.fromisoformat(identity.disclosed_on) <= asof and usable_document(document)


def enable_fact_acquisition(database: Path, *, asof: date) -> None:
    with closing(open_connection(database)) as connection, connection:
        merge_scope(connection, FactsScope(since=asof))
