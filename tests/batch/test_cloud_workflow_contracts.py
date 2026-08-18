from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
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


def _web_history_repo(tmp_path: Path) -> tuple[Path, str]:
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


def test_lake_acceptance_premerge_trigger_is_owner_approved_and_event_bound() -> None:
    workflow = _workflow("lake-acceptance.yml")
    triggers = workflow["on"]
    assert triggers == {
        "pull_request": {"types": ["labeled"]},
        "workflow_dispatch": {
            "inputs": {
                "expected_sha": {
                    "description": "Exact 40-character commit SHA under acceptance",
                    "required": "true",
                    "type": "string",
                }
            }
        },
    }
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"actual-r2"}
    job = jobs["actual-r2"]
    assert isinstance(job, dict)
    condition = str(job["if"])
    for required in (
        "github.event_name == 'workflow_dispatch'",
        "github.event_name == 'pull_request'",
        "github.event.action == 'labeled'",
        "github.event.label.name == 'lake-acceptance-approved'",
        "github.actor == github.repository_owner",
        "github.event.pull_request.head.repo.full_name == github.repository",
    ):
        assert required in condition
    steps = _steps(workflow, "actual-r2")
    checkout = steps[0]
    assert checkout["with"] == {"fetch-depth": "0", "persist-credentials": "false"}
    validation = steps[1]
    assert validation["env"] == {
        "EXPECTED_DISPATCH_SHA": "${{ inputs.expected_sha }}",
        "EXPECTED_PR_SHA": "${{ github.event.pull_request.head.sha }}",
    }
    assert steps[-1]["name"] == "Run actual R2 acceptance"
    assert not any("secrets." in str(step) for step in steps[:-1])


def test_history_workflow_fills_the_store_before_it_reads_or_extends_it() -> None:
    """The object in R2 carries none of the history this pass is meant to extend.

    Without the fill the pass reads an empty coverage window, re-fetches everything a
    previous dispatch already got, and then cannot publish at all — the store push
    empties the lake-owned tables against a release nothing has named. The daily batch
    never exposed this because it does not run the merge, so the ordering is pinned
    here rather than left to the next reader of the workflow.
    """

    steps = _steps(_workflow("cloud-history-backfill.yml"), "backfill")
    names = [str(step.get("name", "")) for step in steps]

    assert "Provision DuckDB httpfs extension" in names
    assert names.index("Provision DuckDB httpfs extension") < names.index("Pull the market store")
    assert names.index("Pull the market store") < names.index(
        "Hydrate market store from the L1 release"
    )
    assert names.index("Hydrate market store from the L1 release") < names.index(
        "Record the window the store already covers"
    )


def test_materialize_workflow_fills_the_store_before_it_reads_it() -> None:
    """The store this workflow pulls carries no market rows at all.

    Every lake-owned table survives the emptying, so the export answers from all of
    them and writes valuations, daily deltas and security views with nothing behind
    them — and the upload then replaces the views the daily batch published from a
    filled store. This runs on every `publish.sh`, so the ordering is pinned rather
    than left to the next reader of the workflow.
    """

    steps = _steps(_workflow("cloud-materialize.yml"), "materialize")
    names = [str(step.get("name", "")) for step in steps]

    assert names.index("Provision DuckDB httpfs extension") < names.index("Pull stores")
    assert names.index("Pull stores") < names.index("Hydrate market store from the L1 release")
    assert names.index("Hydrate market store from the L1 release") < names.index(
        "Materialize read models"
    )
    assert names.index("Materialize read models") < names.index("Upload serving objects")


def test_materialize_provisions_the_extension_before_any_credential_reaches_a_step() -> None:
    """A missing extension has to fail as setup, with nothing published, rather than
    part-way through a run that already holds credentials."""

    steps = _steps(_workflow("cloud-materialize.yml"), "materialize")
    provision = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Provision DuckDB httpfs extension"
    )
    credentialed = [
        index for index, step in enumerate(steps) if "secrets." in str(step.get("env", {}))
    ]

    assert credentialed
    assert provision < min(credentialed)


def test_history_workflow_delegates_partial_failure_publication_to_tested_tool() -> None:
    steps = _steps(_workflow("cloud-history-backfill.yml"), "backfill")
    backfill = next(
        step for step in steps if step.get("name") == "Backfill and publish committed progress"
    )
    run = str(backfill["run"])

    assert "uv run baibai-batch history-backfill" in run
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
        "${{ success() "
        "&& (github.event_name == 'push' || github.event_name == 'workflow_dispatch') "
        "&& github.ref == 'refs/heads/main' "
        "&& steps.deploy-target.outputs.current == 'true' }}"
    )
    assert deploy["run"] == "npx wrangler deploy"


def test_nothing_answering_from_outside_the_tree_stands_in_front_of_production() -> None:
    """`npm audit` answers from GitHub's advisory database, not from the tree.

    A disclosure lands without anyone touching the repository, so an audit in this
    job would turn someone else's publication into an outage on a fix that has
    nothing to do with it. Lockfile resolutions are refused by node-audit.yml,
    whose trigger is the lockfiles themselves.
    """
    steps = _steps(_workflow("web.yml"), "quality")

    assert not [step for step in steps if "audit" in str(step.get("run", ""))]


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


def test_only_named_events_can_reach_production() -> None:
    """`github.ref` is the default branch for schedule, workflow_run and friends.

    Testing "not a pull request" would therefore hand production to any trigger
    added to `on:` later. The condition names the two events that may deploy, and
    the trigger set is pinned so a third one cannot arrive unnoticed.
    """
    workflow = _workflow("web.yml")
    triggers = workflow["on"]
    assert isinstance(triggers, dict)
    by_name = {str(step.get("name", "")): step for step in _steps(workflow, "quality")}

    assert set(triggers) == {"pull_request", "push", "workflow_dispatch"}
    for gated in ("Verify current production target", "Deploy Worker and UI assets"):
        condition = str(by_name[gated]["if"])
        assert (
            "(github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
        ) in condition
        assert "github.ref == 'refs/heads/main'" in condition


def test_web_trigger_covers_every_tree_the_job_publishes() -> None:
    """Editing this workflow is gated before merge but never publishes by itself.

    On `push` the filter is exactly what `wrangler deploy` ships, so a CI-only
    edit cannot reach production; on `pull_request` the file is included so its
    own change still has to pass the job it defines.
    """
    triggers = _workflow("web.yml")["on"]
    assert isinstance(triggers, dict)
    published = ["web/frontend/**", "web/edge/**"]

    for event in ("pull_request", "push"):
        scope = triggers[event]
        assert isinstance(scope, dict)
        assert scope["branches"] == ["main"]
    pull_request = triggers["pull_request"]
    push = triggers["push"]
    assert isinstance(pull_request, dict)
    assert isinstance(push, dict)
    assert pull_request["paths"] == [*published, ".github/workflows/web.yml"]
    assert push["paths"] == published


def test_tracked_tree_stays_inside_the_path_filter_evaluation_limit() -> None:
    """Keep web.yml's `paths` filter exact rather than quietly selective.

    GitHub evaluates the filter against the first 3,000 files of the generated
    diff and skips the workflow, with no check left behind to notice, when a
    matching file falls outside that window. A diff is bounded by the files
    present before it plus the files present after — deletions are why it can
    name more files than the tree holds — so while every commit keeps the tree
    under this budget no diff between two of them can reach 3,000. The largest
    first-parent diff on main so far is 708 files against a tree of 640.

    Growing past this budget means the filter has to give way to in-job detection
    before it starts hiding the web gates. GitHub's troubleshooting page still
    quotes the older 300-file window, so a shrinking limit would also be silent;
    the workflow-syntax reference is the one that carries 3,000.
    """
    tracked = _git(ROOT, "ls-files").splitlines()

    assert len(tracked) < 1000


def test_runs_that_cannot_deploy_stay_out_of_the_production_queue() -> None:
    """A concurrency group holds one pending run and evicts the previous one.

    A dispatch on a feature branch sharing the production group would therefore
    cancel a queued deploy, leaving production behind main with only a notice
    annotation on a green run as evidence.
    """
    workflow = _workflow("web.yml")
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    quality = jobs["quality"]
    assert isinstance(quality, dict)
    concurrency = workflow["concurrency"]
    assert isinstance(concurrency, dict)
    group = str(concurrency["group"])

    assert set(jobs) == {"quality"}
    assert "needs" not in quality
    assert "concurrency" not in quality
    assert "github.event.pull_request.number" in group
    assert "github.ref == 'refs/heads/main' && format('{0}-production-deploy'" in group
    assert "format('{0}-{1}', github.workflow, github.run_id)" in group
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_docs_only_advance_keeps_web_target_current(tmp_path: Path) -> None:
    repo, target = _web_history_repo(tmp_path)
    _commit(repo, "web/frontend/index.ts", "export {};\n", "web target")
    target = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "docs.txt", "current\n", "advance docs only")

    code, output = _run_deploy_target(repo, target=target)

    assert code == 0
    assert output == "current=true\n"


def test_later_web_change_marks_old_target_stale(tmp_path: Path) -> None:
    repo, _baseline = _web_history_repo(tmp_path)
    target = _commit(repo, "web/frontend/index.ts", "export const version = 1;\n", "web target")
    _commit(repo, "web/frontend/index.ts", "export const version = 2;\n", "newer web target")

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
    assert len(setup_steps) == 6
    assert all("version" not in step.get("with", {}) for step in setup_steps)
