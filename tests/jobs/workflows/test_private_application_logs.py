"""Private application diagnostics never become public workflow output."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
import yaml

from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_engine.tasks.service import TaskService

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


@pytest.mark.parametrize("invalid_task", [False, True])
def test_real_materialize_protects_private_validation_and_preserves_success(
    tmp_path, application_workflow, invalid_task
):
    name, steps, logs = application_workflow
    canary = "SYNTHETIC_PRIVATE_TASK_PAYLOAD_12345"
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "engine/src/baibai_engine").mkdir(parents=True)
    app = tmp_path / "stores/application/baibai.sqlite"
    app.parent.mkdir(parents=True)
    TaskService(app).add(
        title=canary, kind="ops", due_date=date(2026, 11, 5), created_at=date(2026, 11, 5)
    )
    if invalid_task:
        with closing(sqlite3.connect(app)) as connection, connection:
            payload = json.loads(connection.execute("SELECT payload FROM task").fetchone()[0])
            payload["ticker"] = canary
            connection.execute("UPDATE task SET payload=?", (json.dumps(payload),))
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
    if invalid_task:
        assert direct.returncode != 0
        assert "ValidationError" in direct.stderr
        assert canary in direct.stderr  # Synthetic reproduction, not a production leak.
    else:
        assert direct.returncode == 0
    results = _run_steps(steps, tmp_path, env)
    assert results[-1].returncode == direct.returncode
    assert canary not in "".join(item.stdout + item.stderr for item in results)
    assert "".join(item.stdout for item in results) == (
        f"portfolio_views_exit_code={direct.returncode}\n" if name == "daily" else ""
    )
    assert all(item.stderr == "" for item in results)
    commands = ["pull-app", "materialize"]
    if not invalid_task:
        commands.append("upload-portfolio-views")
        for source in (tmp_path / "direct/views").iterdir():
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
    if invalid_task:
        assert canary in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600
