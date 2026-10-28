from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest

from baibai_engine.data_collection.receipts.native_sources.documents import SourceDocument, store_document
from baibai_engine.data_collection.receipts.native_sources.handoff import EconomicReceiptConflict, accept_rechecks
from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database


@pytest.mark.parametrize("kind", ["record", "document", "run"])
def test_receipt_identity_conflict_is_owned_by_collection_and_does_not_change_inputs(
    tmp_path, kind
):
    raw, canonical, accepted = (
        tmp_path / name for name in ("raw.sqlite", "canonical.sqlite", "accepted.sqlite")
    )
    stamp = datetime(2026, 10, 15, 7, tzinfo=UTC)
    for path, payload in ((raw, "A"), (canonical, "B")):
        with closing(initialize_database(path)) as connection, connection:
            if kind == "record":
                store_snapshot(
                    connection,
                    source_id="test",
                    observed_at=date(2026, 10, 15),
                    fetched_at=stamp,
                    records=[{"value": payload}],
                )
            elif kind == "document":
                store_document(
                    connection,
                    SourceDocument(
                        "test",
                        "request",
                        "https://example.test/doc",
                        "text/plain",
                        stamp,
                        payload.encode(),
                        {},
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO economic_source_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "run",
                        "test",
                        "2026-10-15",
                        "2026-10-15",
                        stamp.isoformat(),
                        stamp.isoformat(),
                        "failed",
                        0,
                        payload,
                    ),
                )
    # This row is selected before any of the conflicting identities. The conflict
    # must roll its INSERT back, not merely leave an initially empty output empty.
    with closing(initialize_database(raw)) as connection, connection:
        store_snapshot(
            connection,
            source_id="first",
            observed_at=date(2026, 10, 14),
            fetched_at=stamp - timedelta(hours=1),
            records=[{"value": "accepted-before-conflict"}],
        )
    before = (raw.read_bytes(), canonical.read_bytes())
    with pytest.raises(EconomicReceiptConflict, match="conflicting economic receipt identity"):
        accept_rechecks(raw, canonical, accepted)
    assert (raw.read_bytes(), canonical.read_bytes()) == before
    # Jobs handles this as a source-local contract failure, not a new storage failure.
    assert issubclass(EconomicReceiptConflict, ValueError)
    with closing(initialize_database(accepted)) as connection:
        for table in (
            "economic_source_records",
            "economic_source_documents",
            "economic_source_runs",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
