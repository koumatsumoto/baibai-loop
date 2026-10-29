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
