import json
import subprocess
from datetime import date

import pytest

import baibai_jobs.integration.source_handoff as source_handoff
from baibai_jobs.maintenance.market import cli as app


@pytest.mark.parametrize("source", source_handoff.SOURCES[1:6])
def test_source_command_preserves_selected_database(source, tmp_path):
    database = tmp_path / "scoped.sqlite"
    command = source_handoff._command(source, date(2026, 10, 24), database=database)
    arguments = app.build_parser().parse_args(command[2:])
    assert arguments.sqlite_path == str(database)


def test_scoped_acquisition_passes_database_to_child_and_export(tmp_path, mocker, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    database = tmp_path / "scoped.sqlite"
    exported = []

    def export(source, asof, since, selected_database, directory, archives):
        exported.append(selected_database)
        (directory / "result.json").write_text(json.dumps({"source": source}))

    def run(command, **kwargs):
        arguments = app.build_parser().parse_args(command[2:])
        assert arguments.sqlite_path == str(database)
        return subprocess.CompletedProcess(command, 0)

    mocker.patch.object(source_handoff, "_export", side_effect=export)
    mocker.patch.object(source_handoff.subprocess, "run", side_effect=run)
    arguments = [
        "acquire",
        "--source",
        "jquants-market",
        "--asof",
        "2026-10-24",
        "--database",
        str(database),
        "--directory",
        str(tmp_path),
    ]
    assert source_handoff.main(arguments) == 0
    assert exported == [database]


def test_resume_accepts_plain_saved_report_and_retains_incomplete_replay(tmp_path, mocker):
    from contextlib import closing
    from datetime import UTC, datetime

    from tests.jobs.test_source_receipts import Store

    from baibai_engine.data_collection.providers.edinet.receipts import retain_list_response
    from baibai_engine.storage.sqlite.source_cache.schema import open_connection

    store = Store()
    mocker.patch.object(source_handoff, "Boto3R2Store", return_value=store)
    source, target = tmp_path / "source.sqlite", tmp_path / "scoped.sqlite"
    stamp = datetime(2026, 10, 24, 7, tzinfo=UTC)
    day = stamp.date()
    with closing(open_connection(source)) as conn:
        receipt_id = retain_list_response(
            conn, doc_date=day, received_at=stamp, body=b"{invalid JSON"
        )
    packet = tmp_path / "packet"
    source_handoff._export("edinet-list", day, stamp, source, packet)
    # This is the existing plain result.json shape, not a serialized ReplayResult.
    saved_report = {
        "source": "edinet-list",
        "asof": day.isoformat(),
        "started_at": stamp.isoformat(),
        "list_receipts": [receipt_id],
        "archives": [],
    }
    assert json.loads((packet / "result.json").read_text()) == saved_report
    assert (
        source_handoff.main(
            [
                "stage",
                "--source",
                "edinet-list",
                "--stage-id",
                "edinet-list-existing",
                "--directory",
                str(packet),
            ]
        )
        == 0
    )
    manifest_key = "coordination/market-sources/edinet-list-existing/handoff.json"
    manifest = store.values[manifest_key].body
    assert json.loads(manifest)["result"] == saved_report
    original_bytes = (packet / "receipts.sqlite").read_bytes()
    scoped_input = tmp_path / "input"
    scoped_input.mkdir()
    input_report = b'{"created_at":"2026-10-24T06:00:00+00:00"}'
    (scoped_input / "result.json").write_bytes(input_report)
    replay = mocker.spy(source_handoff, "_replay")
    assert (
        source_handoff.main(
            [
                "resume",
                "--source",
                "edinet-list",
                "--asof",
                day.isoformat(),
                "--database",
                str(target),
                "--directory",
                str(scoped_input),
            ]
        )
        == 0
    )
    assert replay.spy_return.complete is False
    assert replay.spy_return.report["failed_days"] == [day.isoformat()]
    downloaded = scoped_input / "resume-edinet-list-0"
    assert json.loads((downloaded / "result.json").read_text()) == saved_report
    assert (downloaded / "receipts.sqlite").read_bytes() == original_bytes
    assert (scoped_input / "result.json").read_bytes() == input_report
    assert store.values[manifest_key].body == manifest
    with closing(open_connection(target)) as conn:
        assert conn.execute("SELECT receipt_id FROM edinet_list_responses").fetchall() == [
            (receipt_id,)
        ]


@pytest.mark.parametrize("group", ["edinet-metrics", "edinet-facts"])
def test_archive_resume_projects_staged_list_without_common_integration(tmp_path, mocker, group):
    from contextlib import closing
    from datetime import UTC, datetime

    from tests.helpers.edinet_handoff_inputs import receive
    from tests.jobs.test_source_receipts import Store

    from baibai_engine.machine_analysis.observations.financials.edinet_models import (
        EDINETProviderError,
    )
    from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
    from baibai_engine.storage.sqlite.source_cache.schema import open_connection
    from baibai_jobs.acquisition.economic_staging import save_handoff

    store = Store()
    mocker.patch.object(source_handoff, "Boto3R2Store", return_value=store)
    stamp = datetime(2026, 10, 29, 7, tzinfo=UTC)
    source, target = tmp_path / "list.sqlite", tmp_path / "scoped.sqlite"
    with closing(open_connection(target)):
        pass
    with pytest.raises(EDINETProviderError, match="2026-10-29"):
        EDINETReader(target).list_documents(stamp.date())
    receive(source, "S100TODAY", stamp, day=stamp.date())
    packet = tmp_path / "packet"
    source_handoff._export("edinet-list", stamp.date(), stamp, source, packet)
    save_handoff(store, "edinet-list-today", packet, market=True)
    originals = {key: value.body for key, value in store.values.items()}
    scoped_input = tmp_path / "input"
    scoped_input.mkdir()
    (scoped_input / "result.json").write_text('{"created_at":"2026-10-28T14:00:00+00:00"}')

    assert (
        source_handoff.main(
            [
                "resume",
                "--source",
                group,
                "--asof",
                stamp.date().isoformat(),
                "--database",
                str(target),
                "--directory",
                str(scoped_input),
            ]
        )
        == 0
    )
    assert [item["docID"] for item in EDINETReader(target).list_documents(stamp.date())] == [
        "S100TODAY"
    ]
    assert not any(key.endswith("/integrated.json") for key in store.values)
    assert {key: value.body for key, value in store.values.items()} == originals


@pytest.mark.parametrize("input_after", [0, 1, 2], ids=["stale", "intermediate", "latest"])
def test_resume_replays_integrated_intermediate_receipts_but_not_unintegrated_late_stage(
    tmp_path, mocker, monkeypatch, input_after
):
    from contextlib import closing
    from datetime import UTC, datetime, timedelta

    from tests.helpers.edinet_handoff_inputs import receive
    from tests.jobs.test_source_receipts import Store

    import baibai_jobs.acquisition.receipts as receipts
    from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import (
        read_edinet_documents,
    )
    from baibai_engine.storage.sqlite.source_cache.schema import open_connection
    from baibai_jobs.acquisition.economic_staging import save_handoff

    store = Store()
    mocker.patch.object(source_handoff, "Boto3R2Store", return_value=store)
    stamp = datetime(2026, 10, 24, 7, tzinfo=UTC)
    integrated_at = stamp

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return integrated_at

    monkeypatch.setattr(receipts, "datetime", Clock)
    canonical, target = tmp_path / "canonical.sqlite", tmp_path / "scoped.sqlite"
    with closing(open_connection(canonical)):
        pass
    snapshots = [canonical.read_bytes()]
    receipt_ids = []
    for index in (1, 2):
        started = stamp + timedelta(days=index)
        source = tmp_path / f"source-{index}.sqlite"
        receipt_ids.append(receive(source, f"S100DAY{index}", started, day=started.date()))
        packet = tmp_path / f"packet-{index}"
        source_handoff._export("edinet-list", started.date(), started, source, packet)
        save_handoff(store, f"edinet-list-{index}", packet, market=True)
        entry = receipts.available_handoffs(store)[0]
        assert source_handoff._replay("edinet-list", canonical, packet).complete is True
        integrated_at = started + timedelta(hours=1)
        receipts.seal_integrated(store, entry, generations={}, recovery=False)
        snapshots.append(canonical.read_bytes())

    # This older stage first arrives after the two successful integrations.
    late_source, late_packet = tmp_path / "late.sqlite", tmp_path / "late"
    receive(late_source, "S100LATE", stamp, day=stamp.date())
    source_handoff._export("edinet-list", stamp.date(), stamp, late_source, late_packet)
    save_handoff(store, "edinet-list-late", late_packet, market=True)
    save_handoff(store, "edinet-list-recovery", late_packet, market=True)
    recovery = next(
        entry
        for entry in receipts.available_handoffs(store)
        if entry["stage_id"] == "edinet-list-recovery"
    )
    receipts.seal_integrated(store, recovery, generations={}, recovery=True)
    pending = receipts.available_handoffs(store)
    assert len(pending) == 1
    assert pending[0]["stage_id"] == "edinet-list-late"
    assert pending[0]["late_before_integrated"] is True
    all_entries = receipts.available_handoffs(store, source="edinet-list", include_integrated=True)
    assert [entry["late_before_integrated"] for entry in all_entries] == [True, False, False, False]
    originals = {key: value.body for key, value in store.values.items()}

    target.write_bytes(snapshots[input_after])
    scoped_input = tmp_path / "input"
    scoped_input.mkdir()
    saved_input = json.dumps(
        {"created_at": (stamp + timedelta(days=input_after, hours=1)).isoformat()}
    ).encode()
    (scoped_input / "result.json").write_bytes(saved_input)
    replay = mocker.spy(source_handoff, "_replay")
    arguments = [
        "resume",
        "--source",
        "edinet-metrics",
        "--asof",
        (stamp + timedelta(days=2)).date().isoformat(),
        "--database",
        str(target),
        "--directory",
        str(scoped_input),
    ]
    for _ in range(2):
        replay.reset_mock()
        assert source_handoff.main(arguments) == 0
        assert [call.args[2].name for call in replay.call_args_list] == [
            f"resume-edinet-list-{index}" for index in range(input_after + 2, 4)
        ]
        for index in (1, 2):
            assert [
                item["docID"]
                for item in read_edinet_documents(target, (stamp + timedelta(days=index)).date())
            ] == [f"S100DAY{index}"]
        with closing(open_connection(target)) as conn:
            assert conn.execute(
                "SELECT receipt_id FROM edinet_list_responses ORDER BY receipt_id"
            ).fetchall() == [(receipt_id,) for receipt_id in sorted(receipt_ids)]
    assert (scoped_input / "result.json").read_bytes() == saved_input
    assert {key: value.body for key, value in store.values.items()} == originals


@pytest.mark.parametrize("source", ["unknown", "jquants-typo", "edinet-typo", "article-unknown"])
def test_unknown_source_never_reaches_an_existing_owner(source, tmp_path, mocker):
    edinet = mocker.patch.object(source_handoff, "import_edinet_handoff")
    jquants = mocker.patch.object(source_handoff, "import_jquants_handoff")
    with pytest.raises(ValueError, match="unsupported"):
        source_handoff._replay(source, tmp_path / "market.sqlite", tmp_path)
    edinet.assert_not_called()
    jquants.assert_not_called()
