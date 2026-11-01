from __future__ import annotations

import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from baibai_engine.machine_analysis.observations.disclosures.tender_offer import edinet_code_to_ticker
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def _store_documents(sqlite_path: Path, rows: list[tuple[object, ...]]) -> None:
    connection = open_connection(sqlite_path)
    connection.executemany(
        "INSERT INTO edinet_documents("
        "doc_date, sequence_number, doc_id, sec_code, doc_type_code, legal_status, "
        "disclosure_status, withdrawal_status, edinet_code, issuer_edinet_code, "
        "subject_edinet_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.executemany(
        "INSERT OR REPLACE INTO edinet_document_lists("
        "doc_date, process_datetime, result_count, selected_received_at_utc, "
        "latest_received_at_utc, rechecked_asof"
        ") VALUES (?, NULL, 1, '2026-08-11T00:00:00+00:00', "
        "'2026-08-11T00:00:00+00:00', '2026-08-11')",
        sorted({(str(row[0]),) for row in rows}),
    )
    connection.commit()
    connection.close()


class EdinetCodeMappingTest(unittest.TestCase):
    ASOF = date(2026, 8, 10)

    def test_identity_mapping_does_not_truncate_nonordinary_or_malformed_codes(self) -> None:
        with TemporaryDirectory() as tmp:
            sqlite_path = Path(tmp) / "market.sqlite"
            _store_documents(
                sqlite_path,
                [
                    ("2026-06-01", 1, "S0001", "13010", "120", "1", "0", "0", "E1", None, None),
                    ("2026-06-01", 2, "S0002", "13011", "120", "1", "0", "0", "E2", None, None),
                    ("2026-06-01", 3, "S0003", "1301bad", "120", "1", "0", "0", "E3", None, None),
                    ("2026-06-01", 4, "S0004", "130a0", "120", "1", "0", "0", "E4", None, None),
                ],
            )
            with closing(open_connection(sqlite_path)) as connection:
                self.assertEqual(
                    edinet_code_to_ticker(connection, through=self.ASOF),
                    {"E1": "1301", "E4": "130A"},
                )

    def test_future_identity_and_conflict_do_not_change_historical_mapping(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.sqlite"
            _store_documents(
                path,
                [
                    ("2026-06-01", 1, "S0001", "13010", "120", "1", "0", "0", "E1", None, None),
                    ("2026-08-03", 1, "S0002", None, "350", "1", "0", "0", "E9", "E1", None),
                    ("2026-08-11", 1, "S0003", "13020", "120", "1", "0", "0", "E1", None, None),
                    ("2026-08-11", 2, "S0004", "13030", "120", "1", "0", "0", "E3", None, None),
                ],
            )
            with closing(open_connection(path)) as connection:
                before = edinet_code_to_ticker(connection, through=self.ASOF)
                after = edinet_code_to_ticker(connection, through=date(2026, 8, 11))
            self.assertEqual(before, {"E1": "1301"})
            self.assertEqual(after, {"E3": "1303"})
