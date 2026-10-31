import json
from types import SimpleNamespace

import pytest

from baibai_engine.data_collection.transport.http import AcquisitionDeadlineExceeded
from baibai_jobs.acquisition import edinet_archives
from baibai_jobs.integration import source_handoff


@pytest.mark.parametrize("source", ["edinet-metrics", "edinet-facts"])
def test_archive_deadline_exports_successful_keys_and_stages_failed_acquisition(
    source, tmp_path, monkeypatch
):
    documents = [SimpleNamespace(doc_id=name) for name in ("S100GOOD", "S100STOP", "S100NEXT")]
    monkeypatch.setattr(edinet_archives, "EDINETReader", lambda _: None)
    monkeypatch.setattr(
        edinet_archives,
        "prepare_edinet_metric_plan",
        lambda **_: SimpleNamespace(archive_candidates=documents),
    )
    monkeypatch.setattr(
        edinet_archives,
        "prepare_edinet_facts",
        lambda **_: SimpleNamespace(documents=documents, statuses={}),
    )
    monkeypatch.setattr(
        edinet_archives,
        "plan_fact_item",
        lambda document, **_: SimpleNamespace(doc_id=document.doc_id, action="extract"),
    )
    retained = set()
    calls = []

    class Acquisition:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

        def download(self, doc_id, source_type):
            calls.append(doc_id)
            if doc_id == "S100STOP":
                raise AcquisitionDeadlineExceeded("acquisition job deadline exhausted")
            retained.add((doc_id, source_type))

        def download_csv_zip(self, doc_id):
            self.download(doc_id, 5)

        def download_xbrl_zip(self, doc_id):
            self.download(doc_id, 1)

    monkeypatch.setattr(edinet_archives, "EDINETRefresh", Acquisition)
    monkeypatch.setattr(source_handoff, "_retained_archive_keys", lambda _: set(retained))
    exported = []

    def export(group, asof, since, database, directory, archive_keys):
        exported.append(set(archive_keys))
        (directory / "result.json").write_text(json.dumps({"source": group}))

    monkeypatch.setattr(source_handoff, "_export", export)
    inputs = tmp_path / "input"
    inputs.mkdir()
    (inputs / "result.json").write_text(
        json.dumps({"archive_keys": [], "canonical_generations": {}})
    )
    destination = tmp_path / "handoff"
    destination.mkdir()
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("EDINET_API_KEY", "synthetic-key")
    result = source_handoff.main(
        [
            "acquire",
            "--source",
            source,
            "--asof",
            "2026-10-24",
            "--database",
            str(tmp_path / "scoped.sqlite"),
            "--input-directory",
            str(inputs),
            "--directory",
            str(destination),
        ]
    )
    assert result == 1
    assert calls == ["S100GOOD", "S100STOP"]
    assert exported == [{("S100GOOD", 5 if source == "edinet-metrics" else 1)}]
    report = json.loads((destination / "result.json").read_text())
    assert report["acquisition"]["downloaded"] == 1
    assert report["acquisition"]["failed"] == ["S100STOP"]
    assert "stage_ready=true" in output.read_text()
