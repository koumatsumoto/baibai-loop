"""Native ZIP acquisition must work when L2 imports and all SQLite opens are forbidden."""

import subprocess
import sys
import textwrap


def test_native_archive_acquisition_never_loads_l2_or_opens_sqlite(tmp_path):
    script = textwrap.dedent(
        """
        import importlib.abc
        import io
        import json
        import sqlite3
        import sys
        import zipfile
        from datetime import UTC, date, datetime
        from pathlib import Path

        class RejectL2(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                if fullname.startswith("baibai_engine.machine_analysis"):
                    raise AssertionError("native acquisition loaded L2: " + fullname)

        sys.meta_path.insert(0, RejectL2())
        def reject_sqlite(*args, **kwargs):
            raise AssertionError("native acquisition opened SQLite")
        sqlite3.connect = reject_sqlite

        from tests.helpers.r2_store import MemoryR2Store
        from baibai_engine.data_collection.providers.edinet.archive_native import EdinetArchiveL1
        from baibai_engine.data_collection.providers.edinet.list_native import EdinetListL1, NativeListReceipt
        from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
        from baibai_jobs.acquisition import edinet_archive_native as acquisition

        day = date(2026, 11, 5)
        at = datetime(2026, 11, 5, 12, tzinfo=UTC)
        store = MemoryR2Store()
        directory = Path(sys.argv[1])
        lists = EdinetListL1(store, directory / "lists")
        lists.freeze(require_existing=False)
        common = {"xbrlFlag": "1", "legalStatus": "1", "withdrawalStatus": "0",
                  "disclosureStatus": "0", "docInfoEditStatus": "0",
                  "submitDateTime": "2026-11-05 10:00"}
        rows = [
            {**common, "seqNumber": 1, "docID": "S100ANNUAL", "secCode": "72030",
             "docTypeCode": "120", "csvFlag": "1", "periodStart": "2025-04-01",
             "periodEnd": "2026-03-31"},
            {**common, "seqNumber": 2, "docID": "S100HOLDER", "docTypeCode": "350"},
        ]
        body = json.dumps({"metadata": {"resultset": {"count": 2}}, "results": rows}).encode()
        fixed_lists = lists.publish([NativeListReceipt.from_response(day, EdinetResponse(at, body))])
        archives = EdinetArchiveL1(store, directory / "archives")
        archives.initialize()
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as bundle:
            bundle.writestr("native.txt", b"same original")
        zip_body = content.getvalue()
        requested = []
        class Client:
            def fetch_archive(self, doc_id, source_type):
                requested.append((doc_id, source_type))
                return EdinetResponse(at, zip_body)
        acquisition.time.sleep = lambda _: None
        for group in ("edinet-metrics", "edinet-facts"):
            report = acquisition.acquire_archives(
                archives, Client(), fixed_lists, group=group, asof=day,
                lookback_days=0, initialize_facts=group == "edinet-facts"
            )
            assert report["status"] == "received"
        assert requested == [("S100ANNUAL", 5), ("S100ANNUAL", 1), ("S100HOLDER", 1)]
        fixed = archives.freeze()
        for doc_id, source_type in requested:
            assert fixed.read_archive(doc_id, source_type) == zip_body
            assert fixed.metadata(doc_id, source_type).received_at_utc == at
        assert fixed.scope.doc_ids == frozenset({"S100ANNUAL"})
        assert fixed.scope.since == day
        assert not list(directory.rglob("*.sqlite"))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
