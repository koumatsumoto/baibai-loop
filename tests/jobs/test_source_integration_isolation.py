import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta

import pytest
from tests.engine.test_jquants_handoff import calendar
from tests.engine.test_jquants_projection import FIRST_AT, body, receive
from tests.jobs.test_source_receipts import Store, stage

from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.observations.jquants_projection import project_request
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError, R2Error
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.acquisition.receipts import available_handoffs, seal_integrated
from baibai_jobs.integration.jquants_handoff import MARKET_SOURCES, export_handoff
from baibai_jobs.integration.replay_result import ReplayResult
from baibai_jobs.integration.sources import apply


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
        "baibai_jobs.integration.sources.extract_saved_edinet",
        lambda *args, **kwargs: {},
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


@pytest.mark.parametrize("projection_failed", [False, True])
def test_incomplete_market_projection_preserves_explicit_recovery_and_watermark(
    tmp_path, monkeypatch, projection_failed
):
    store = Store()
    market, economic = tmp_path / "market.sqlite", tmp_path / "economic.sqlite"
    for path, initialize in ((market, open_connection), (economic, initialize_database)):
        with closing(initialize(path)):
            pass
    packet = tmp_path / "packet"
    packet.mkdir()
    with closing(open_connection(packet / "receipts.sqlite")):
        pass
    report = {
        "source": "jpx-regulations",
        "asof": "2026-10-24",
        "started_at": FIRST_AT.isoformat(),
    }
    (packet / "result.json").write_text(json.dumps(report))
    save_handoff(store, "jpx-partial", packet, market=True)
    work = tmp_path / "integration"
    work.mkdir()
    (work / "plan.json").write_text(json.dumps({"handoffs": available_handoffs(store)}))
    monkeypatch.setattr(
        "baibai_jobs.integration.sources._replay",
        lambda *args, **kwargs: ReplayResult(
            report={"projection_failures": ["bundle"] if projection_failed else []},
            complete=not projection_failed,
        ),
    )
    monkeypatch.setattr(
        "baibai_jobs.integration.sources.extract_saved_edinet", lambda *args, **kwargs: {}
    )
    outcome = apply(store, work, market=market, economic=economic, asof=date(2026, 10, 24))[
        "handoffs"
    ][0]
    assert outcome["applied"] is True
    assert outcome["failed"] is projection_failed
    assert outcome["recovery"] is projection_failed
    seal_integrated(store, outcome["entry"], generations={}, recovery=outcome["recovery"])
    assert available_handoffs(store) == []
    explicit = available_handoffs(store, include_integrated=True)
    assert explicit[0]["integration"]["status"] == (
        "needs_explicit_recovery" if projection_failed else "integrated"
    )
    report["started_at"] = (FIRST_AT - timedelta(hours=1)).isoformat()
    (packet / "result.json").write_text(json.dumps(report))
    save_handoff(store, "jpx-earlier", packet, market=True)
    assert available_handoffs(store)[0]["late_before_integrated"] is (not projection_failed)


def _mixed_handoffs(tmp_path, monkeypatch, *, corrupt_market=False, market_report=None):
    store = Store()
    market, economic = tmp_path / "market.sqlite", tmp_path / "economic.sqlite"
    for path, initialize in ((market, open_connection), (economic, initialize_database)):
        with closing(initialize(path)):
            pass
    packet = tmp_path / "market-packet"
    packet.mkdir()
    if corrupt_market:
        (packet / "receipts.sqlite").write_bytes(b"not a SQLite receipt")
    else:
        with closing(open_connection(packet / "receipts.sqlite")):
            pass
    (packet / "result.json").write_text(
        json.dumps(
            market_report
            or {
                "source": "edinet-list",
                "asof": "2026-10-24",
                "started_at": FIRST_AT.isoformat(),
                "list_receipts": [],
                "archives": [],
            }
        )
    )
    save_handoff(store, "edinet-list-fault", packet, market=True)
    stage(store, tmp_path / "good", "fred_csv-good", "fred_csv", value="independent receipt")
    entries = available_handoffs(store)
    entries.sort(key=lambda entry: not entry["market"])
    work = tmp_path / "integration"
    work.mkdir()
    (work / "plan.json").write_text(json.dumps({"handoffs": entries}))
    monkeypatch.setattr(
        "baibai_jobs.integration.sources.extract_saved_edinet", lambda *args, **kwargs: {}
    )
    return store, work, market, economic


@pytest.mark.parametrize(
    ("operation", "error", "fatal"),
    [
        ("download_fixed", R2Error("transport unavailable"), True),
        ("download_fixed", LakeCASConflict("conditional generation changed"), True),
        ("download_fixed", LakePublishError("R2 is only source-contract text"), False),
        ("_replay", sqlite3.DatabaseError("canonical corruption"), True),
        ("_replay", sqlite3.OperationalError("canonical storage unavailable"), True),
        ("_replay", OSError("canonical I/O failed"), True),
        ("_replay", sqlite3.IntegrityError("source identity conflicts"), False),
    ],
)
def test_apply_preserves_global_and_source_local_error_scope(
    tmp_path, monkeypatch, operation, error, fatal
):
    import baibai_jobs.integration.sources as integration

    store, work, market, economic = _mixed_handoffs(tmp_path, monkeypatch)
    original = getattr(integration, operation)

    def fail_market(*args, **kwargs):
        if operation == "_replay" or args[1]["market"]:
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(integration, operation, fail_market)
    if fatal:
        with pytest.raises(type(error)):
            apply(store, work, market=market, economic=economic, asof=date(2026, 10, 24))
        assert not (work / "applied.json").exists()
    else:
        report = apply(store, work, market=market, economic=economic, asof=date(2026, 10, 24))
        assert report["handoffs"][0]["result"] == {"reason": type(error).__name__}
        assert report["handoffs"][0]["failed"] is True
        assert report["handoffs"][1]["failed"] is False
        with closing(initialize_database(economic)) as conn:
            assert next(
                read_snapshots(
                    conn, date(2026, 10, 24), date(2026, 10, 24), source_id="fred-payems"
                )
            ).records == ({"value": "independent receipt"},)


def test_invalid_receipt_sqlite_is_local_and_other_source_still_integrates(tmp_path, monkeypatch):
    store, work, market, economic = _mixed_handoffs(tmp_path, monkeypatch, corrupt_market=True)
    report = apply(store, work, market=market, economic=economic, asof=date(2026, 10, 24))
    assert report["handoffs"][0]["result"] == {"reason": "ValueError"}
    assert report["handoffs"][0]["failed"] is True
    assert report["handoffs"][1]["failed"] is False


@pytest.mark.parametrize("field", ["receipt_sha256", "receipt_bytes", "receipt_key"])
def test_incomplete_handoff_manifest_is_local_and_other_source_still_integrates(
    tmp_path, monkeypatch, field
):
    store, work, market, economic = _mixed_handoffs(tmp_path, monkeypatch)
    key = "coordination/market-sources/edinet-list-fault/handoff.json"
    manifest = json.loads(store.values[key].body)
    del manifest[field]
    store.store_object(key, json.dumps(manifest).encode())
    entries = available_handoffs(store)
    entries.sort(key=lambda entry: not entry["market"])
    (work / "plan.json").write_text(json.dumps({"handoffs": entries}))

    report = apply(store, work, market=market, economic=economic, asof=date(2026, 10, 24))
    bad, good = report["handoffs"]
    assert bad["result"] == {"reason": "ValueError"}
    assert bad["applied"] is False
    assert bad["failed"] is bad["recovery"] is True
    assert good["applied"] is True
    assert good["failed"] is False
    with closing(initialize_database(economic)) as conn:
        assert next(
            read_snapshots(conn, date(2026, 10, 24), date(2026, 10, 24), source_id="fred-payems")
        ).records == ({"value": "independent receipt"},)


@pytest.mark.parametrize(
    ("source", "metadata"),
    [
        ("jquants-market", {}),
        ("jquants-financial", {"request_ids": [{}]}),
        ("jpx-regulations", {}),
        ("jpx-earnings", {"bundle_ids": "not-an-id-list"}),
        ("tradingview", {"symbols": [], "receipt_ids": [], "master_request_ids": []}),
        (
            "tradingview",
            {
                "snapshot_date": "2026-10-24",
                "master_asof": "2026-10-23",
                "symbols": [],
                "receipt_ids": [None],
                "master_request_ids": [],
            },
        ),
    ],
)
def test_invalid_market_metadata_is_local_and_other_source_still_integrates(
    tmp_path, monkeypatch, source, metadata
):
    store, work, market, economic = _mixed_handoffs(
        tmp_path,
        monkeypatch,
        market_report={
            "source": source,
            "asof": "2026-10-24",
            "started_at": FIRST_AT.isoformat(),
            **metadata,
        },
    )
    report = apply(store, work, market=market, economic=economic, asof=date(2026, 10, 24))
    bad, good = report["handoffs"]
    assert bad["result"] == {"reason": "ValueError"}
    assert bad["applied"] is False
    assert bad["failed"] is bad["recovery"] is True
    assert good["applied"] is True
    assert good["failed"] is False
    with closing(initialize_database(economic)) as conn:
        assert next(
            read_snapshots(conn, date(2026, 10, 24), date(2026, 10, 24), source_id="fred-payems")
        ).records == ({"value": "independent receipt"},)
