import json
import subprocess
from contextlib import closing
from datetime import date, timedelta

from tests.batch.test_source_receipts import Store, stage
from tests.engine.test_jquants_handoff import calendar
from tests.engine.test_jquants_projection import FIRST_AT, receive, body

from baibai_batch.jobs.source_integration import apply
from baibai_batch.storage.economic_staging import save_handoff
from baibai_batch.storage.source_receipts import available_handoffs
from baibai_engine.collection.economic.records import read_snapshots
from baibai_engine.collection.economic.store import initialize_database
from baibai_engine.market.jquants_handoff import MARKET_SOURCES, export_handoff
from baibai_engine.market.jquants_projection import project_request
from baibai_engine.storage.observations.schema import open_connection


def test_failed_jquants_candidate_keeps_prior_projection_and_integrates_other_source(
    tmp_path, monkeypatch
):
    store = Store()
    market, source = tmp_path / "market.sqlite", tmp_path / "source.sqlite"
    prior = calendar(market, "2026-10-01", "2026-10-10", 1, FIRST_AT)
    project_request(market, prior)
    # A syntactically received page is not a complete admissible calendar view.
    receive(
        source,
        "market_calendar",
        {"from": "2026-10-01", "to": "2026-10-10"},
        body([{"Date": "2026-10-01"}]),
        FIRST_AT + timedelta(hours=1),
    )
    packet = tmp_path / "bad"
    packet.mkdir()
    requests = export_handoff(
        source, packet / "receipts.sqlite", since=FIRST_AT, sources=MARKET_SOURCES
    )
    (packet / "result.json").write_text(
        json.dumps(
            {
                "source": "jquants-market",
                "asof": "2026-10-24",
                "started_at": FIRST_AT.isoformat(),
                "request_ids": requests,
            }
        )
    )
    save_handoff(store, "jquants-market-bad", packet, market=True)
    stage(store, tmp_path / "good", "fred_csv-good", "fred_csv", value="independent receipt")
    entries = available_handoffs(store)
    entries.sort(key=lambda entry: not entry["market"])
    work = tmp_path / "integration"
    work.mkdir()
    (work / "plan.json").write_text(json.dumps({"handoffs": entries}))
    economic = tmp_path / "economic.sqlite"
    with closing(initialize_database(economic)):
        pass
    with closing(open_connection(market)) as conn:
        before_rows = conn.execute("SELECT * FROM jquants_market_calendar ORDER BY day").fetchall()
        before_coverage = conn.execute(
            "SELECT * FROM source_coverage ORDER BY source, coverage_key"
        ).fetchall()
    monkeypatch.setattr(
        "baibai_batch.jobs.source_integration.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    report = apply(store, work, market=market, economic=economic, asof=date(2026, 10, 24))
    assert report["handoffs"][0]["failed"] is True
    assert report["handoffs"][1]["failed"] is False
    with closing(open_connection(market)) as conn:
        assert (
            conn.execute("SELECT * FROM jquants_market_calendar ORDER BY day").fetchall()
            == before_rows
        )
        assert (
            conn.execute("SELECT * FROM source_coverage ORDER BY source, coverage_key").fetchall()
            == before_coverage
        )
        assert (
            conn.execute("SELECT COUNT(DISTINCT request_id) FROM jquants_source_pages").fetchone()[
                0
            ]
            == 2
        )
    with closing(initialize_database(economic)) as conn:
        assert next(
            read_snapshots(conn, date(2026, 10, 24), date(2026, 10, 24), source_id="fred-payems")
        ).records == ({"value": "independent receipt"},)
