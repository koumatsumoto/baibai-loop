"""経済原受領の有限handoffを作る合成fixture。実storeを参照しない。"""

import json
from contextlib import closing

from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_jobs.acquisition.economic_staging import save_handoff


def stage(
    store, directory, stage_id, group="boj", *, started_at="2026-10-24T07:43:00+00:00", value=None
):
    directory.mkdir()
    from datetime import date, datetime

    from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot

    with closing(initialize_database(directory / "receipts.sqlite")) as conn, conn:
        if value is not None:
            store_snapshot(
                conn,
                source_id="fred-payems",
                observed_at=date(2026, 10, 24),
                fetched_at=datetime.fromisoformat(started_at),
                records=[{"value": value}],
            )
    (directory / "result.json").write_text(
        json.dumps(
            {
                "group": group,
                "asof": "2026-10-24",
                "started_at": started_at,
                "sources": [],
            }
        )
    )
    save_handoff(store, stage_id, directory)

