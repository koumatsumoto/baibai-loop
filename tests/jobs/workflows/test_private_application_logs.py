"""Private application diagnostics never become public workflow output."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
import yaml
from tests.helpers.db_seed import seed_ledger
from tests.helpers.ledger import load_portfolio_ledger
from tests.helpers.outcome_publication import _publication

from baibai_engine.portfolio.performance.store import PortfolioOutcomeStore
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(params=["daily", "materialize"])
def application_workflow(request):
    name = request.param
    path = ROOT / ".github/workflows" / f"cloud-{'daily-batch' if name == 'daily' else name}.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"][name]["steps"]
    selected = [
        step
        for step in steps
        if step.get("id") in {"portfolio-views", "pull-app", "materialize"}
        or step.get("name") == "Upload serving objects"
    ]
    logs = (
        dict.fromkeys(
            ("pull-app", "materialize", "upload-portfolio-views"), "portfolio-views-private.log"
        )
        if name == "daily"
        else {
            "pull-app": "materialize-pull-app-private.log",
            "materialize": "materialize-private.log",
            "upload-portfolio-views": "materialize-upload-private.log",
        }
    )
    for log in set(logs.values()):
        assert sum(str(step).count(log) for step in steps) == 1
    return name, selected, logs


def _portfolio_commands(tmp_path, *, real_materialize=False):
    script = tmp_path / "jobs/scripts/r2_transfer.sh"
    script.parent.mkdir(parents=True)
    script.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$1" >> "$RUNNER_TEMP/calls"\n'
        'printf "synthetic-private-stdout-%s\\n" "$1"\n'
        'printf "synthetic-private-stderr-%s\\n" "$1" >&2\n'
        '[ "${FAIL_AT:-}" != "$1" ] || exit 23\n'
    )
    script.chmod(0o755)
    binary = tmp_path / "bin"
    binary.mkdir()
    uv = binary / "uv"
    uv.write_text(
        '#!/bin/sh\necho materialize >> "$RUNNER_TEMP/calls"\n'
        + (
            'shift 2\nexec "$TEST_PYTHON" "$@"\n'
            if real_materialize
            else "echo synthetic-private-stdout-materialize\n"
            "echo synthetic-private-stderr-materialize >&2\n"
            '[ "${FAIL_AT:-}" != "materialize" ] || exit 23\n'
        )
    )
    uv.chmod(0o755)
    return {
        **os.environ,
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "RUNNER_TEMP": str(tmp_path),
        "TEST_PYTHON": sys.executable,
        "MARKET_OUTCOME": "success",
        "PYTHONPATH": os.pathsep.join(
            str(ROOT / path) for path in ("engine/src", "jobs/src", "web/backend/src")
        ),
    }


def _run_steps(steps, tmp_path, env):
    results = []
    for step in steps:
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", step["run"]],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        results.append(result)
        if result.returncode:
            break
    return results


@pytest.mark.parametrize("fail_at", ["", "pull-app", "materialize", "upload-portfolio-views"])
def test_command_output_stays_private_and_failure_stops_later_commands(
    tmp_path, application_workflow, fail_at
):
    name, steps, logs = application_workflow
    results = _run_steps(steps, tmp_path, {**_portfolio_commands(tmp_path), "FAIL_AT": fail_at})
    expected = 23 if fail_at else 0
    assert results[-1].returncode == expected
    assert "".join(item.stdout for item in results) == (
        f"portfolio_views_exit_code={expected}\n" if name == "daily" else ""
    )
    assert all(item.stderr == "" for item in results)
    commands = ["pull-app", "materialize", "upload-portfolio-views"]
    if fail_at:
        commands = commands[: commands.index(fail_at) + 1]
    assert (tmp_path / "calls").read_text().splitlines() == commands
    for command in commands:
        log = tmp_path / logs[command]
        assert log.stat().st_mode & 0o777 == 0o600
        assert f"synthetic-private-stdout-{command}" in log.read_text()
        assert f"synthetic-private-stderr-{command}" in log.read_text()


@pytest.mark.parametrize("invalid_outcome", [False, True])
def test_real_materialize_protects_private_validation_and_preserves_success(
    tmp_path, application_workflow, invalid_outcome
):
    name, steps, logs = application_workflow
    canary = "SYNTHETIC_PRIVATE_OUTCOME_PAYLOAD_12345"
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "engine/src/baibai_engine").mkdir(parents=True)
    app = tmp_path / "stores/application/baibai.sqlite"
    app.parent.mkdir(parents=True)
    seed_ledger(
        app,
        load_portfolio_ledger(ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"),
    )
    PortfolioOutcomeStore(app).publish(_publication())
    if invalid_outcome:
        with closing(sqlite3.connect(app)) as connection, connection:
            payload = json.loads(
                connection.execute("SELECT payload FROM portfolio_outcome").fetchone()[0]
            )
            payload["portfolio_twr_pct"] = canary
            connection.execute("UPDATE portfolio_outcome SET payload=?", (json.dumps(payload),))
    market = tmp_path / "stores/market/market.sqlite"
    market.parent.mkdir(parents=True)
    with closing(open_connection(market)):
        pass
    before = app.read_bytes()
    env = _portfolio_commands(tmp_path, real_materialize=True)
    direct = subprocess.run(
        [sys.executable, "-m", "baibai_web.materialize", "--output-dir", str(tmp_path / "direct")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert direct.returncode == 0
    assert canary not in direct.stdout + direct.stderr
    dashboard = json.loads((tmp_path / "direct/views/portfolio-dashboard.json").read_text())
    assert dashboard["ledger_error"] is None
    assert dashboard["holdings"][0]["quantity"] == 200
    outcomes = json.loads((tmp_path / "direct/views/outcomes.json").read_text())
    if invalid_outcome:
        assert outcomes == {"error": "保存済み運用成績を読み取れません。", "outcomes": []}
    else:
        assert outcomes["error"] is None
        assert outcomes["outcomes"][0]["portfolio_twr_pct"] == 10.2
    results = _run_steps(steps, tmp_path, env)
    assert results[-1].returncode == direct.returncode
    assert canary not in "".join(item.stdout + item.stderr for item in results)
    assert "".join(item.stdout for item in results) == (
        f"portfolio_views_exit_code={direct.returncode}\n" if name == "daily" else ""
    )
    assert all(item.stderr == "" for item in results)
    commands = ["pull-app", "materialize", "upload-portfolio-views"]
    for directory in ("direct", "serving"):
        assert {source.name for source in (tmp_path / directory / "views").iterdir()} == {
            "portfolio-dashboard.json",
            "outcomes.json",
            "latest-proposal.json",
        }
    for source in (tmp_path / "direct/views").iterdir():
        assert canary not in source.read_text()
        expected = json.loads(source.read_text())
        actual = json.loads((tmp_path / "serving/views" / source.name).read_text())
        # Each invocation stamps its own read time; every application field is identical.
        if "generated_at" in expected:
            assert actual.pop("generated_at")
            assert expected.pop("generated_at")
        assert actual == expected
    assert (tmp_path / "calls").read_text().splitlines() == commands
    assert app.read_bytes() == before
    log = tmp_path / logs["materialize"]
    assert canary not in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600


def test_real_materialize_write_failure_stays_private_and_stops_upload(
    tmp_path, application_workflow
):
    name, steps, logs = application_workflow
    canary = "SYNTHETIC_PRIVATE_OUTPUT_PATH_12345"
    root = tmp_path / canary
    root.mkdir()
    (root / "pyproject.toml").write_text("")
    (root / "engine/src/baibai_engine").mkdir(parents=True)
    for directory in ("direct", "serving"):
        output = root / directory
        output.mkdir()
        (output / "views").write_text("synthetic output obstruction")
    env = _portfolio_commands(root, real_materialize=True)
    direct = subprocess.run(
        [sys.executable, "-m", "baibai_web.materialize", "--output-dir", str(root / "direct")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert direct.returncode != 0
    assert "FileExistsError" in direct.stderr
    assert canary in direct.stderr  # Real writer failure with a synthetic private path.

    results = _run_steps(steps, root, env)

    assert results[-1].returncode == direct.returncode
    assert "".join(item.stdout for item in results) == (
        f"portfolio_views_exit_code={direct.returncode}\n" if name == "daily" else ""
    )
    assert all(item.stderr == "" for item in results)
    assert canary not in "".join(item.stdout + item.stderr for item in results)
    assert (root / "calls").read_text().splitlines() == ["pull-app", "materialize"]
    log = root / logs["materialize"]
    assert "FileExistsError" in log.read_text()
    assert canary in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600
