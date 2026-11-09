"""JPX取得は空inputを経由せず、成功・部分受領を既存stageへ保持する。"""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
import requests
from tests.helpers.http_doubles import FakeSession, html_response, response
from tests.helpers.jpx_earnings import FILE_A, FIXTURES, SOURCE_FILES, _workbook
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jpx.http import JPXHTTP
from baibai_engine.data_collection.providers.jpx.receipts import read_bundle
from baibai_engine.machine_analysis.observations.disclosures.jpx_sources import (
    JPX_EARNINGS_CALENDAR_INDEX_URL,
    JPX_REGULATION_URLS,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration import source_handoff

STAMP = datetime(2026, 10, 15, 7, tzinfo=UTC)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return STAMP.astimezone(tz) if tz else STAMP.replace(tzinfo=None)


def refuse(*args, **kwargs):
    raise AssertionError("external connection or source input was requested")


@pytest.mark.parametrize("source", ["jpx-earnings", "jpx-regulations"])
@pytest.mark.parametrize("failure", ["none", "first_http", "partial_http", "parse"])
def test_actual_acquisition_entrypoint_and_stage(source, failure, tmp_path, monkeypatch):
    for key in ("JQUANTS_API_KEY", "EDINET_API_KEY", "BAIBAI_ACQUISITION_DEADLINE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(requests, "Session", refuse)
    monkeypatch.setattr(source_handoff, "Boto3R2Store", refuse)
    monkeypatch.setattr(source_handoff, "datetime", Clock)
    monkeypatch.setattr("baibai_jobs.acquisition.jpx_native.datetime", Clock)
    monkeypatch.setattr("baibai_engine.data_collection.providers.jpx.http.datetime", Clock)
    if source == "jpx-earnings":
        answers = {
            JPX_EARNINGS_CALENDAR_INDEX_URL: html_response(b"<a href='kessan.xlsx'>daily</a>"),
            FILE_A: response(
                _workbook("", [["2026-10-20", "130A"]]),
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        }
        if failure == "parse":
            answers[FILE_A] = response(
                b"invalid workbook",
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
    else:
        answers = {
            url: html_response((FIXTURES / SOURCE_FILES[name]).read_bytes())
            for name, url in JPX_REGULATION_URLS.items()
        }
        if failure == "parse":
            answers[list(answers)[-1]] = html_response(b"not a JPX table")
    if failure in {"first_http", "partial_http"}:
        answers[list(answers)[failure == "partial_http"]] = response(status_code=503)
    session = FakeSession(by_url=answers)
    monkeypatch.setattr(
        "baibai_engine.data_collection.providers.jpx.http.JPXHTTP",
        lambda: JPXHTTP(session=session),
    )
    db, packet, output = tmp_path / "fresh.sqlite", tmp_path / "packet", tmp_path / "github-output"
    assert not db.exists()
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    code = source_handoff.main(
        [
            "acquire",
            "--source",
            source,
            "--asof",
            STAMP.date().isoformat(),
            "--database",
            str(db),
            "--directory",
            str(packet),
        ]
    )
    assert code == (failure in {"first_http", "partial_http"})
    assert output.read_text() == "stage_ready=true\n"
    report = json.loads((packet / "result.json").read_bytes())
    assert "input_generation" not in report
    assert report["acquisition"]["status"] == (
        "failed" if failure in {"first_http", "partial_http"} else "received"
    )
    assert len(report["bundle_ids"]) == 1
    bundle_id = report["bundle_ids"][0]
    expected_count = (
        0 if failure == "first_http" else 1 if failure == "partial_http" else len(answers)
    )
    assert not db.exists()
    with closing(sqlite3.connect(packet / "receipts.sqlite")) as exported:
        received = read_bundle(exported, bundle_id)
        assert len(received.responses) == expected_count
        assert (received.completed_at is not None) == (failure in {"none", "parse"})
        assert (
            exported.execute(
                "SELECT name FROM sqlite_schema WHERE name='source_coverage'"
            ).fetchone()
            is None
        )
    store = MemoryR2Store()
    monkeypatch.setattr(source_handoff, "Boto3R2Store", lambda **kwargs: store)
    stage = source + "-probe"
    assert (
        source_handoff.main(
            ["stage", "--source", source, "--stage-id", stage, "--directory", str(packet)]
        )
        == 0
    )
    saved = {key: value.body for key, value in store.values.items()}
    downloaded, target = tmp_path / "downloaded", tmp_path / "canonical.sqlite"
    assert (
        source_handoff.main(
            ["download", "--source", source, "--stage-id", stage, "--directory", str(downloaded)]
        )
        == 0
    )
    before_http = list(session.calls)
    for repeat in range(2):
        if repeat:
            # Old generation-bearing packets remain valid for idempotent replay.
            report["input_generation"] = {"market": "previous-code-generation"}
            (downloaded / "result.json").write_text(json.dumps(report))
        replay_code = source_handoff.main(
            [
                "replay-copy",
                "--source",
                source,
                "--database",
                str(target),
                "--directory",
                str(downloaded),
            ]
        )
        assert replay_code == (failure != "none")
        with closing(open_connection(target)) as canonical:
            assert read_bundle(canonical, bundle_id) == received
            assert canonical.execute("SELECT COUNT(*) FROM jpx_source_bundles").fetchone()[0] == 1
    assert session.calls == before_http
    assert {key: value.body for key, value in store.values.items()} == saved
    assert not any("source-inputs" in key for key in store.values)
