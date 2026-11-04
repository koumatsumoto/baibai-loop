from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime

import pytest

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsPage,
)
from baibai_jobs.acquisition.jquants_financial_receive import receive_financial_days


class Client:
    def __init__(self, *, partial: bool = False) -> None:
        self.partial = partial

    def pages(self, source, params):
        yield JQuantsPage(
            source,
            params,
            datetime(2026, 11, 4, tzinfo=UTC),
            json.dumps(
                {"data": [], **({"pagination_key": "next"} if self.partial else {})}
            ).encode(),
        )
        if self.partial:
            raise JQuantsAcquisitionError("interrupted")


def test_receives_only_new_originals(tmp_path) -> None:
    output = tmp_path / "receipts.sqlite"
    ids = receive_financial_days(output, [date(2026, 11, 2)], client=Client())
    with sqlite3.connect(output) as conn:
        assert conn.execute("SELECT source, query_json FROM jquants_source_pages").fetchall() == [
            ("financial_summaries", '{"date":"20261102"}')
        ]
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name = ?", ("financial_summaries",)
            ).fetchone()
            is None
        )
    assert len(ids) == 1


def test_interrupted_request_keeps_received_page(tmp_path) -> None:
    output = tmp_path / "receipts.sqlite"
    with pytest.raises(JQuantsAcquisitionError):
        receive_financial_days(output, [date(2026, 11, 2)], client=Client(partial=True))
    with sqlite3.connect(output) as conn:
        assert conn.execute("SELECT count(*) FROM jquants_source_pages").fetchone()[0] == 1
