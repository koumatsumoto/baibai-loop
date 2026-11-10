"""Exact JPX recovery retains failed-stage evidence and bypasses no source checks."""

import json
from contextlib import closing
from datetime import UTC, date, datetime

import pytest
from tests.helpers.edinet_native import empty_edinet_input
from tests.helpers.jpx_earnings import FILE_A, FIXTURES, SOURCE_FILES, _workbook
from tests.helpers.master_calendar_native import seed_calendar, seed_master
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jpx.models import JPXResponse
from baibai_engine.data_collection.providers.jpx.receipt_store import open_receipts
from baibai_engine.data_collection.providers.jpx.receipts import (
    complete_bundle,
    create_bundle,
    retain_bundle_response,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_sources import (
    JPX_EARNINGS_CALENDAR_INDEX_URL,
    JPX_REGULATION_URLS,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.economic_staging import save_handoff, stage_prefix
from baibai_jobs.acquisition.receipts import available_handoffs, seal_integrated
from baibai_jobs.integration import sources

DAY = date(2026, 10, 15)
STAMP = datetime(2026, 10, 15, 7, tzinfo=UTC)


@pytest.fixture
def failed_stages(tmp_path):
    store = MemoryR2Store()
    selections = []
    for source, collection in (
        ("jpx-earnings", "earnings_calendar"),
        ("jpx-regulations", "regulations"),
    ):
        packet = tmp_path / source
        if source == "jpx-earnings":
            answers = {
                "index": (
                    JPX_EARNINGS_CALENDAR_INDEX_URL,
                    "text/html",
                    b"<a href='kessan.xlsx'>daily</a>",
                ),
                "workbook:0": (
                    FILE_A,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    _workbook("", [["2026-10-20", "130A"]]),
                ),
            }
        else:
            answers = {
                name: (url, "text/html", (FIXTURES / SOURCE_FILES[name]).read_bytes())
                for name, url in JPX_REGULATION_URLS.items()
            }
        with closing(open_receipts(packet / "receipts.sqlite")) as conn:
            bundle = create_bundle(
                conn, collection, DAY, STAMP, tuple((name, row[0]) for name, row in answers.items())
            )
            for name, (url, media, body) in answers.items():
                retain_bundle_response(conn, bundle, name, JPXResponse(url, STAMP, media, body))
            complete_bundle(conn, bundle)
        (packet / "result.json").write_text(
            json.dumps(
                {
                    "source": source,
                    "asof": str(DAY),
                    "started_at": STAMP.isoformat(),
                    "bundle_ids": [bundle],
                }
            )
        )
        stage = source + "-failed"
        save_handoff(store, stage, packet, market=True)
        entry = next(row for row in available_handoffs(store) if row["stage_id"] == stage)
        seal_integrated(store, entry, generations={"lake_release_id": "before"}, recovery=True)
        selections.append([source, stage, entry["manifest_sha256"]])
    assert available_handoffs(store) == []
    return store, selections


def test_exact_jpx_plan_replays_and_seals_without_rewriting_original_failure(
    tmp_path, monkeypatch, failed_stages
):
    store, selections = failed_stages
    original_objects = {key: row.body for key, row in store.values.items()}
    empty_edinet_input(store, tmp_path)
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: store)

    def no_discovery(*args, **kwargs):
        pytest.fail("exact recovery must not discover other pending sources or ready inputs")

    for name in ("available_handoffs", "pending_handoffs", "available_ready"):
        monkeypatch.setattr(sources, name, no_discovery)
    work = tmp_path / "recovery"
    args = ["plan", "--directory", str(work)]
    for selection in selections:
        args.extend(["--recover-jpx-stage", *selection])
    assert sources.main(args) == 0
    plan = json.loads((work / "plan.json").read_text())
    assert plan["external"] == plan["external_failed"] == []
    assert len(plan["handoffs"]) == 2
    for entry, (source, stage, digest) in zip(plan["handoffs"], selections, strict=True):
        assert entry["source"] == source
        assert entry["stage_id"] == stage
        assert entry["manifest_sha256"] == digest
        assert entry["started_at"] == STAMP.isoformat()
        assert entry["explicit_jpx_recovery"] is True
    fixed_plan = (work / "plan.json").read_bytes()
    assert sources.main(args) == 1
    assert (work / "plan.json").read_bytes() == fixed_plan

    market = tmp_path / "market.sqlite"
    with closing(open_connection(market)):
        pass
    seed_calendar(store, market, tmp_path / "calendar", as_of=DAY)
    seed_master(store, market, tmp_path / "master", as_of=DAY)
    for repeat in range(2):
        applied = sources.apply(store, work, market=market, asof=DAY)
        assert all(row["applied"] and not row["failed"] for row in applied["handoffs"]), applied[
            "handoffs"
        ]
        for row in applied["handoffs"]:
            assert (row["result"]["raw_rows_added"] == 0) is bool(repeat)
            seal_integrated(
                store, row["entry"], generations={"lake_release_id": "after"}, recovery=False
            )
        for key, body in original_objects.items():
            assert store.values[key].body == body
    with closing(open_connection(market)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jpx_source_bundles").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM jpx_earnings_calendar").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM jpx_regulation_flags").fetchone()[0] > 0
    for _, stage, digest in selections:
        key = stage_prefix(stage, market=True) + f"/recovered-{digest}.json"
        receipt = json.loads(store.values[key].body)
        assert receipt["manifest_sha256"] == digest
        assert receipt["canonical_generations"] == {"lake_release_id": "after"}
    with pytest.raises(ValueError, match="already has"):
        sources._jpx_recovery_handoffs(store, selections)


@pytest.mark.parametrize("fault", ["digest", "source", "marker", "duplicate"])
def test_exact_jpx_recovery_rejects_mismatched_selection_without_writes(failed_stages, fault):
    store, selections = failed_stages
    if fault == "digest":
        selections[0][2] = "f" * 64
    elif fault == "source":
        selections[0][0] = "jpx-regulations"
    elif fault == "marker":
        key = stage_prefix(selections[0][1], market=True) + "/integrated.json"
        marker = json.loads(store.values[key].body)
        marker["status"] = "integrated"
        store.store_object(key, json.dumps(marker).encode())
    else:
        selections.append(selections[0])
    before = {key: row.body for key, row in store.values.items()}
    with pytest.raises((ValueError, sources.LakePublishError)):
        sources._jpx_recovery_handoffs(store, selections)
    assert {key: row.body for key, row in store.values.items()} == before


@pytest.mark.parametrize("warm", [False, True])
def test_jpx_recovery_keeps_full_fixed_edinet_withdrawal_projection(tmp_path, failed_stages, warm):
    from datetime import timedelta

    from tests.jobs.acquisition.test_edinet_list_native_route import payload, receipt

    from baibai_engine.data_collection.providers.edinet.archive_native import EdinetArchiveL1
    from baibai_engine.data_collection.providers.edinet.list_native import EdinetListL1
    from baibai_engine.machine_analysis.generation.edinet_inputs import retain_edinet_input
    from baibai_jobs.integration.edinet_list_native import materialize_lists

    store, selections = failed_stages
    lists = EdinetListL1(store, tmp_path / "edinet-lists")
    lists.freeze(False)
    first = {
        "seqNumber": 1,
        "docID": "S100TEST",
        "legalStatus": "1",
        "secCode": "20000",
        "withdrawalStatus": "0",
    }
    original = receipt(DAY, at=STAMP, body=payload(DAY, rows=[first]))
    before = lists.publish([original])
    market = tmp_path / "market.sqlite"
    with closing(open_connection(market)):
        pass
    if warm:
        materialize_lists(before, market)
    withdrawal = receipt(
        DAY,
        at=STAMP + timedelta(seconds=1),
        body=payload(
            DAY,
            rows=[
                {"seqNumber": 1, "docID": "S100TEST", "withdrawalStatus": "2", "legalStatus": "0"}
            ],
        ),
    )
    corrected = lists.publish([withdrawal])
    archives = EdinetArchiveL1(store, tmp_path / "edinet-archives").initialize()
    saved = retain_edinet_input(
        store, corrected, archives, selected_at=STAMP + timedelta(seconds=2)
    )
    work = tmp_path / "apply"
    work.mkdir()
    (work / "plan.json").write_text(
        json.dumps(
            {
                "handoffs": sources._jpx_recovery_handoffs(store, selections),
                "external": [],
                "external_failed": [],
                "edinet_input": saved.model_dump(mode="json"),
            }
        )
    )
    seed_calendar(store, market, tmp_path / "calendar", as_of=DAY)
    seed_master(store, market, tmp_path / "master", as_of=DAY)
    result = sources.apply(store, work, market=market, asof=DAY)
    assert all(row["applied"] and not row["failed"] for row in result["handoffs"])
    assert result["edinet_input"] == saved.model_dump(mode="json")
    with closing(open_connection(market)) as conn:
        assert conn.execute(
            "SELECT sec_code,withdrawal_status,legal_status FROM edinet_documents WHERE doc_id='S100TEST'"
        ).fetchone() == ("20000", "2", "0")
        rows = conn.execute(
            "SELECT receipt_id,received_at_utc,body_gzip FROM edinet_list_responses ORDER BY received_at_utc"
        ).fetchall()
        assert rows == [
            (item.receipt_id, item.received_at.isoformat(timespec="microseconds"), item.body_gzip)
            for item in (original, withdrawal)
        ]
        assert conn.execute("SELECT COUNT(*) FROM jpx_source_bundles").fetchone()[0] == 2
