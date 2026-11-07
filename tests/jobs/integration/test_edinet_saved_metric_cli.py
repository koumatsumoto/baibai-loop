"""An explicit saved pair builds a fresh candidate without changing offline extraction."""

import json
import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pytest
from tests.helpers.edinet_metrics import _edinet_csv_zip
from tests.jobs.integration.test_edinet_metric_rebuild import (
    ARCHIVE_HEAD,
    AT,
    BASE,
    LIST_HEAD,
    document,
    sources,
)

from baibai_engine.data_collection.providers.edinet.facts_scope import FactsScope, read_scope
from baibai_engine.machine_analysis.generation.edinet_inputs import retain_edinet_input
from baibai_engine.machine_analysis.observations.financials.edinet_reader import EDINETReader
from baibai_jobs import cli as jobs_cli
from baibai_jobs.integration import edinet_metric_rebuild as app


def _saved(tmp_path, monkeypatch, *, mode="completed"):
    docs = [document(), document("S100OTHER", "83060")]
    bodies = {("S100FACT", 1): b"unrelated XBRL"}
    if mode not in {"deferred_input"}:
        bodies[("S100ONE", 5)] = b"not a ZIP" if mode == "failed" else _edinet_csv_zip()
    if mode == "completed":
        bodies[("S100OTHER", 5)] = _edinet_csv_zip()
    store, _, owner, lists, archives = sources(tmp_path, days={BASE: docs}, bodies=bodies)
    archives = owner.publish([], scope=FactsScope(None, frozenset({"S100FACT"})))
    saved = retain_edinet_input(store, lists, archives, selected_at=AT)
    input_file = tmp_path / "saved-input.json"
    input_file.write_text(saved.model_dump_json())
    del store.values[LIST_HEAD]
    del store.values[ARCHIVE_HEAD]
    store.close = Mock()
    factory = Mock(return_value=store)
    monkeypatch.setattr(app, "Boto3R2Store", factory)
    monkeypatch.setattr(
        EDINETReader, "read_csv_zip", Mock(side_effect=AssertionError("SQL ZIP read"))
    )
    store.get_keys.clear()
    store.head_keys.clear()
    store.put_keys.clear()
    return store, saved, input_file, factory


def _argv(input_file, output):
    return [
        "--saved-input",
        str(input_file),
        "--output",
        str(output),
        "--asof",
        BASE.isoformat(),
        "--lookback-days",
        "0",
    ]


@pytest.mark.parametrize("status", ["completed", "partial", "deferred_input", "failed"])
def test_cli_preserves_saved_identity_clocks_and_existing_candidate_outcomes(
    tmp_path, monkeypatch, capsys, status
):
    store, saved, input_file, _ = _saved(tmp_path, monkeypatch, mode=status)
    fact_key = next(key for key, value in store.values.items() if value.body == b"unrelated XBRL")
    before = dict(store.values)
    output = tmp_path / "candidate.sqlite"
    written = status in {"completed", "partial"}
    assert jobs_cli.main(["rebuild-edinet-metrics", *_argv(input_file, output)]) == int(not written)
    report = json.loads(capsys.readouterr().out)
    assert report["edinet-metrics"]["status"] == status
    assert report["output_written"] is written
    assert report["edinet_input"] == saved.model_dump(mode="json")
    assert report["selected_at"] == AT.isoformat()
    assert report["requested_asof"] == BASE.isoformat()
    assert report["selected_at"][:10] != report["requested_asof"]
    assert output.exists() is written
    if written:
        with closing(sqlite3.connect(output)) as connection:
            assert connection.execute("SELECT count(*) FROM edinet_source_archives").fetchone() == (
                0,
            )
            assert connection.execute("SELECT count(*) FROM edinet_segment_facts").fetchone() == (
                0,
            )
            assert read_scope(connection) == FactsScope()
    assert store.values == before
    assert not store.put_keys
    assert not {LIST_HEAD, ARCHIVE_HEAD}.intersection(store.get_keys + store.head_keys)
    assert fact_key not in store.get_keys
    store.close.assert_called_once_with()


@pytest.mark.parametrize("failure", ["metadata_missing", "body_changed", "output_exists"])
def test_cli_never_repairs_saved_inputs_or_overwrites_an_existing_output(
    tmp_path, monkeypatch, capsys, failure
):
    store, _, input_file, _ = _saved(tmp_path, monkeypatch)
    output = tmp_path / "candidate.sqlite"
    if failure == "metadata_missing":
        del store.values[next(key for key in store.values if "/selections/" in key)]
    elif failure == "body_changed":
        key = next(
            key for key in store.values if key.endswith(".zip") and len(store.values[key].body) > 20
        )
        store.store_object(key, b"changed")
    else:
        output.write_bytes(b"existing candidate")
    before = dict(store.values)
    assert app.main(_argv(input_file, output)) == 1
    assert capsys.readouterr().out == ""
    assert store.values == before
    assert not store.put_keys
    assert not {LIST_HEAD, ARCHIVE_HEAD}.intersection(store.get_keys + store.head_keys)
    if failure == "output_exists":
        assert output.read_bytes() == b"existing candidate"
    else:
        assert not output.exists()
    store.close.assert_called_once_with()


def test_bad_saved_descriptor_never_opens_a_store(tmp_path, monkeypatch, capsys):
    input_file = tmp_path / "bad.json"
    input_file.write_text("{}")
    factory = Mock(side_effect=AssertionError("store opened without a saved identity"))
    monkeypatch.setattr(app, "Boto3R2Store", factory)
    assert app.main(_argv(input_file, tmp_path / "candidate.sqlite")) == 1
    assert capsys.readouterr().out == ""
    factory.assert_not_called()


def test_saved_pair_is_required_and_help_opens_no_store(tmp_path, monkeypatch, capsys):
    factory = Mock(side_effect=AssertionError("store opened while parsing arguments"))
    monkeypatch.setattr(app, "Boto3R2Store", factory)
    with pytest.raises(SystemExit) as missing:
        app.main(["--output", str(tmp_path / "candidate.sqlite"), "--asof", BASE.isoformat()])
    assert missing.value.code == 2
    with pytest.raises(SystemExit) as help_exit:
        jobs_cli.main(["rebuild-edinet-metrics", "--help"])
    assert help_exit.value.code == 0
    assert "--saved-input" in capsys.readouterr().out
    factory.assert_not_called()
