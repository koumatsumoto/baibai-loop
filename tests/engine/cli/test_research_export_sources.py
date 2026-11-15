"""選定 context の出力先は、読んだ入力 DB と別のファイルでなければならない。"""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tests.helpers.model_generation import empty_selection, generation, publish_generation

from baibai_engine.cli.investment_judgment import main
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore

NOW = datetime(2026, 10, 25, 0, 2, tzinfo=UTC)


def _context(tmp_path):
    runs, app = tmp_path / "runs.sqlite", tmp_path / "app.sqlite"
    fixed = generation()
    publish_generation(fixed, path=runs)
    ScreeningSelectionStore(runs).publish("fixed-selection", empty_selection(fixed.generation_id))
    with closing(sqlite3.connect(runs)) as connection, connection:
        connection.execute(
            "UPDATE screening_selection SET published_at=?",
            ((NOW - timedelta(minutes=1)).isoformat(),),
        )
    seed_ledger(
        app, load_portfolio_ledger(Path("tests/fixtures/portfolio-ledger/representative.yaml"))
    )
    argv = [
        "selection-context",
        "--request",
        "合成入力の保存先を確認する",
        "--scope",
        "discover_new",
        "--screening-selection-id",
        "fixed-selection",
        "--runs-path",
        str(runs),
        "--db",
        str(app),
    ]
    return runs, app, argv


@pytest.mark.parametrize("source", ["runs", "app"])
@pytest.mark.parametrize("alias", ["direct", "relative", "symlink", "hardlink"])
def test_context_export_cannot_replace_its_input(tmp_path, monkeypatch, capsys, source, alias):
    runs, app, argv = _context(tmp_path)
    target = runs if source == "runs" else app
    output = target
    if alias == "relative":
        monkeypatch.chdir(tmp_path)
        output = Path(target.name)
    elif alias != "direct":
        output = tmp_path / "alias.sqlite"
        if alias == "symlink":
            output.symlink_to(target)
        else:
            output.hardlink_to(target)
    before = runs.read_bytes(), app.read_bytes(), output.read_bytes()

    assert main([*argv, "--out", str(output)], now=NOW) == 3

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "invalid_source"
    assert result["reason"] == "selection context export must not replace an input database"
    assert (runs.read_bytes(), app.read_bytes(), output.read_bytes()) == before
    assert output.is_symlink() == (alias == "symlink")


@pytest.mark.parametrize("source", ["runs", "app"])
def test_context_export_cannot_create_json_at_an_absent_input(tmp_path, capsys, source):
    target = tmp_path / source
    argv = [
        "selection-context",
        "--request",
        "合成未作成入力",
        "--scope",
        "discover_new",
        "--runs-path",
        str(tmp_path / "runs"),
        "--db",
        str(tmp_path / "app"),
        "--out",
        str(target),
    ]
    assert main(argv, now=NOW) == 3
    assert json.loads(capsys.readouterr().out)["reason"] == (
        "selection context export must not replace an input database"
    )
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "app").exists()


@pytest.mark.parametrize("path_source", ["environment", "default"])
def test_context_export_protects_resolved_application_path(
    tmp_path, monkeypatch, capsys, path_source
):
    from baibai_engine.storage.sqlite.application import paths

    runs, app, argv = _context(tmp_path)
    index = argv.index("--db")
    del argv[index : index + 2]
    if path_source == "environment":
        monkeypatch.setenv("BAIBAI_DB", str(app))
    else:
        monkeypatch.delenv("BAIBAI_DB", raising=False)
        monkeypatch.setattr(paths, "DEFAULT_DB_PATH", app)
    before = runs.read_bytes(), app.read_bytes()
    assert main([*argv, "--out", str(app)], now=NOW) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "invalid_source"
    assert (runs.read_bytes(), app.read_bytes()) == before


def test_context_export_protects_default_runs_path(tmp_path, monkeypatch, capsys):
    from baibai_engine.cli import investment_judgment

    runs, app, argv = _context(tmp_path)
    index = argv.index("--runs-path")
    del argv[index : index + 2]
    monkeypatch.setattr(investment_judgment, "RUNS_DB_PATH", runs)
    before = runs.read_bytes(), app.read_bytes()
    assert main([*argv, "--out", str(runs)], now=NOW) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "invalid_source"
    assert (runs.read_bytes(), app.read_bytes()) == before


def test_context_export_keeps_normal_replacement_and_stdout(tmp_path, capsys):
    runs, app, argv = _context(tmp_path)
    before = runs.read_bytes(), app.read_bytes()
    output = tmp_path / "context.json"
    for _ in range(2):
        assert main([*argv, "--out", str(output)], now=NOW) == 0
        assert output.read_text() == capsys.readouterr().out
        assert json.loads(output.read_text())["status"] == "ready"
        assert (runs.read_bytes(), app.read_bytes()) == before
