from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def _workflow(name: str) -> dict[str, object]:
    loaded = yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def _steps(workflow: dict[str, object], job: str) -> list[dict[str, object]]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    selected = jobs[job]
    assert isinstance(selected, dict)
    steps = selected["steps"]
    assert isinstance(steps, list)
    return steps


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _run_deploy_scope(repo: Path, *, before: str, current: str) -> tuple[int, str]:
    steps = _steps(_workflow("web.yml"), "scope")
    scope = next(step for step in steps if step.get("id") == "deploy-scope")
    output = repo / "github-output"
    output.unlink(missing_ok=True)
    env = os.environ.copy()
    env.update(
        {
            "EVENT_NAME": "push",
            "GIT_REF": "refs/heads/main",
            "PUSH_BEFORE_SHA": before,
            "CURRENT_SHA": current,
            "GITHUB_OUTPUT": str(output),
        }
    )
    result = subprocess.run(
        ("bash", "-e", "-u", "-o", "pipefail", "-c", str(scope["run"])),
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    written = output.read_text(encoding="utf-8") if output.exists() else ""
    return result.returncode, written


def _run_deploy_target(repo: Path, *, target: str) -> tuple[int, str]:
    steps = _steps(_workflow("web.yml"), "quality")
    target_step = next(step for step in steps if step.get("id") == "deploy-target")
    output = repo / "github-output"
    output.unlink(missing_ok=True)
    env = os.environ.copy()
    env.update({"TARGET_SHA": target, "GITHUB_OUTPUT": str(output)})
    result = subprocess.run(
        ("bash", "-e", "-u", "-o", "pipefail", "-c", str(target_step["run"])),
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    written = output.read_text(encoding="utf-8") if output.exists() else ""
    return result.returncode, written


def _deploy_scope_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Workflow Test")
    _git(repo, "config", "user.email", "workflow@example.invalid")
    baseline = _commit(repo, "docs.txt", "baseline\n", "baseline")
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", str(repo))
    return repo, baseline


@pytest.mark.parametrize(
    "workflow_name",
    ["cloud-daily-batch.yml", "cloud-materialize.yml", "cloud-history-backfill.yml"],
)
def test_cloud_writers_share_one_non_cancelling_fifo_queue(workflow_name: str) -> None:
    concurrency = _workflow(workflow_name)["concurrency"]

    assert concurrency == {
        "group": "cloud-publish",
        "queue": "max",
        "cancel-in-progress": "false",
    }


def test_history_workflow_delegates_partial_failure_publication_to_tested_tool() -> None:
    steps = _steps(_workflow("cloud-history-backfill.yml"), "backfill")
    backfill = next(
        step for step in steps if step.get("name") == "Backfill and publish committed progress"
    )
    run = str(backfill["run"])

    assert "python -m tools.cloud.history_backfill" in run
    assert "--master-month-end-from" in run
    assert not any(step.get("name") == "Upload the market store" for step in steps)


def test_web_workflow_keeps_all_gates_before_the_only_deploy_step() -> None:
    steps = _steps(_workflow("web.yml"), "quality")
    names = [str(step.get("name", "")) for step in steps]
    gates = [
        "Lint UI",
        "Build UI",
        "Test UI",
        "Check Worker generated types",
        "Typecheck Worker",
        "Test Worker",
        "Dry-run Worker deploy",
    ]
    deploy_index = names.index("Deploy Worker and UI assets")

    assert all(names.index(gate) < deploy_index for gate in gates)
    assert names.index("Verify current production target") < deploy_index
    assert names.count("Deploy Worker and UI assets") == 1
    deploy = steps[deploy_index]
    assert deploy["if"] == (
        "${{ success() && needs.scope.outputs.deploy == 'true' "
        "&& steps.deploy-target.outputs.current == 'true' }}"
    )
    assert deploy["run"] == "npx wrangler deploy"


@pytest.mark.parametrize(
    "failed_gate",
    [
        "Lint UI",
        "Build UI",
        "Test UI",
        "Check Worker generated types",
        "Typecheck Worker",
        "Test Worker",
        "Dry-run Worker deploy",
    ],
)
def test_each_failed_web_gate_leaves_deploy_call_count_zero(failed_gate: str) -> None:
    steps = _steps(_workflow("web.yml"), "quality")
    success = True
    deploy_calls = 0
    for step in steps:
        name = str(step.get("name", ""))
        if name == failed_gate:
            success = False
        if name == "Deploy Worker and UI assets" and success:
            deploy_calls += 1

    assert deploy_calls == 0


def test_manual_non_main_dispatch_cannot_enable_deploy() -> None:
    steps = _steps(_workflow("web.yml"), "scope")
    scope = next(step for step in steps if step.get("id") == "deploy-scope")
    run = str(scope["run"])

    assert "deploy=false" in run
    assert '"$EVENT_NAME" == "workflow_dispatch"' in run
    assert '"$GIT_REF" == "refs/heads/main"' in run
    assert "workflow_dispatch" in _workflow("web.yml")["on"]


def test_deploy_scope_uses_full_history_and_fails_closed() -> None:
    steps = _steps(_workflow("web.yml"), "scope")
    checkout = next(
        step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    scope = next(step for step in steps if step.get("id") == "deploy-scope")

    assert checkout["with"] == {"fetch-depth": "0"}
    assert "push before SHA is unavailable" in str(scope["run"])
    assert "failed to determine production deploy scope" in str(scope["run"])


def test_multi_commit_non_web_push_does_not_deploy(tmp_path: Path) -> None:
    repo, before = _deploy_scope_repo(tmp_path)
    _commit(repo, "docs.txt", "first\n", "first docs change")
    current = _commit(repo, "docs.txt", "second\n", "second docs change")

    code, output = _run_deploy_scope(repo, before=before, current=current)

    assert code == 0
    assert output == "deploy=false\n"


def test_multi_commit_push_with_web_change_deploys(tmp_path: Path) -> None:
    repo, before = _deploy_scope_repo(tmp_path)
    _commit(repo, "ui/index.ts", "export {};\n", "web change")
    current = _commit(repo, "docs.txt", "after web\n", "follow-up docs change")

    code, output = _run_deploy_scope(repo, before=before, current=current)

    assert code == 0
    assert output == "deploy=true\n"


def test_unavailable_push_before_sha_fails_without_deploy(tmp_path: Path) -> None:
    repo, _before = _deploy_scope_repo(tmp_path)
    current = _git(repo, "rev-parse", "HEAD")

    code, output = _run_deploy_scope(repo, before="0" * 40, current=current)

    assert code != 0
    assert "deploy=true" not in output


def test_only_deploy_eligible_quality_jobs_share_production_lock() -> None:
    workflow = _workflow("web.yml")
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    quality = jobs["quality"]
    assert isinstance(quality, dict)
    concurrency = quality["concurrency"]
    assert isinstance(concurrency, dict)
    group = str(concurrency["group"])

    assert quality["needs"] == "scope"
    assert "needs.scope.outputs.deploy == 'true'" in group
    assert "production-deploy" in group
    assert "github.run_id" in group
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_docs_only_advance_keeps_web_target_current(tmp_path: Path) -> None:
    repo, target = _deploy_scope_repo(tmp_path)
    _commit(repo, "ui/index.ts", "export {};\n", "web target")
    target = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "docs.txt", "current\n", "advance docs only")

    code, output = _run_deploy_target(repo, target=target)

    assert code == 0
    assert output == "current=true\n"


def test_later_web_change_marks_old_target_stale(tmp_path: Path) -> None:
    repo, _baseline = _deploy_scope_repo(tmp_path)
    target = _commit(repo, "ui/index.ts", "export const version = 1;\n", "web target")
    _commit(repo, "ui/index.ts", "export const version = 2;\n", "newer web target")

    code, output = _run_deploy_target(repo, target=target)

    assert code == 0
    assert output == "current=false\n"


def test_cloudflare_credentials_are_scoped_to_deploy_step_only() -> None:
    workflow = _workflow("web.yml")
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    quality = jobs["quality"]
    assert isinstance(quality, dict)
    assert "env" not in quality
    steps = _steps(workflow, "quality")
    secret_steps = [
        step
        for step in steps
        if "CLOUDFLARE_API_TOKEN" in str(step.get("env", {}))
        or "CLOUDFLARE_ACCOUNT_ID" in str(step.get("env", {}))
    ]

    assert [step["name"] for step in secret_steps] == ["Deploy Worker and UI assets"]


def test_legacy_independent_deploy_workflow_is_removed() -> None:
    assert not (WORKFLOWS / "cloud-deploy.yml").exists()


def test_every_setup_uv_step_resolves_one_exact_root_version() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    required = config["tool"]["uv"]["required-version"]
    setup_steps: list[dict[str, object]] = []
    for path in WORKFLOWS.glob("*.yml"):
        workflow = _workflow(path.name)
        jobs = workflow["jobs"]
        assert isinstance(jobs, dict)
        for job in jobs.values():
            assert isinstance(job, dict)
            for step in job["steps"]:
                if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
                    setup_steps.append(step)

    assert required == "==0.12.1"
    assert len(setup_steps) == 5
    assert all("version" not in step.get("with", {}) for step in setup_steps)
