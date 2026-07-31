import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "security.yml"


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
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


def _run_detector(
    repo: Path,
    *,
    event_name: str,
    base_sha: str = "",
    head_sha: str = "",
) -> str:
    steps = _workflow()["jobs"]["audit"]["steps"]
    detector = next(step for step in steps if step.get("id") == "node-audit-scope")
    output = repo / "github-output"
    output.unlink(missing_ok=True)
    env = os.environ | {
        "EVENT_NAME": event_name,
        "PR_BASE_SHA": base_sha,
        "PR_HEAD_SHA": head_sha,
        "PUSH_BEFORE_SHA": base_sha,
        "CURRENT_SHA": head_sha,
        "GITHUB_OUTPUT": str(output),
    }
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", detector["run"]],
        cwd=repo,
        check=True,
        env=env,
    )
    return output.read_text(encoding="utf-8").strip()


def test_security_workflow_keeps_all_repository_events_visible() -> None:
    triggers = _workflow()["on"]

    assert triggers["pull_request"]["branches"] == ["main"]
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["schedule"] == [{"cron": "17 3 * * 1"}]
    assert "paths" not in triggers["pull_request"]
    assert "paths-ignore" not in triggers["pull_request"]
    assert "paths" not in triggers["push"]
    assert "paths-ignore" not in triggers["push"]


def test_node_audit_is_fail_closed_and_reuses_the_security_job() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["audit"]["steps"]
    by_name = {step.get("name"): step for step in steps if "name" in step}
    checkout = next(step for step in steps if step.get("uses") == "actions/checkout@v7")
    setup_node = next(step for step in steps if step.get("uses") == "actions/setup-node@v7")
    detector = by_name["Detect Node audit scope"]

    assert set(workflow["jobs"]) == {"audit"}
    assert checkout["with"]["fetch-depth"] == "2"
    assert detector["id"] == "node-audit-scope"
    assert "ui/package-lock.json cloud/worker/package-lock.json" in " ".join(
        detector["run"].split()
    )

    node_if = "${{ !cancelled() && steps.node-audit-scope.outputs.run == 'true' }}"
    assert setup_node["if"] == node_if
    assert by_name["UI dependency audit"]["if"] == node_if
    assert by_name["Worker dependency audit"]["if"] == node_if
    assert steps.index(by_name["Dependency audit"]) < steps.index(setup_node)
    assert steps.index(setup_node) < steps.index(by_name["UI dependency audit"])
    assert steps.index(by_name["UI dependency audit"]) < steps.index(
        by_name["Worker dependency audit"]
    )
    audit_command = "npm audit --package-lock-only --audit-level=high"
    assert by_name["UI dependency audit"]["run"] == audit_command
    assert by_name["Worker dependency audit"]["run"] == audit_command


def test_node_audit_detector_routes_each_event_fail_closed(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "CI test")
    _git(tmp_path, "config", "user.email", "ci-test@example.invalid")
    _commit(tmp_path, "ui/package-lock.json", '{"lockfileVersion": 3}\n', "UI lock")
    base = _commit(
        tmp_path,
        "cloud/worker/package-lock.json",
        '{"lockfileVersion": 3}\n',
        "Worker lock",
    )
    unrelated = _commit(tmp_path, "README.md", "documentation\n", "Docs")
    ui_changed = _commit(
        tmp_path,
        "ui/package-lock.json",
        '{"lockfileVersion": 3, "changed": true}\n',
        "Update UI lock",
    )
    worker_changed = _commit(
        tmp_path,
        "cloud/worker/package-lock.json",
        '{"lockfileVersion": 3, "changed": true}\n',
        "Update Worker lock",
    )

    cases = [
        ("schedule", "", "", "run=true"),
        ("pull_request", base, unrelated, "run=false"),
        ("pull_request", base, ui_changed, "run=true"),
        ("push", base, unrelated, "run=false"),
        ("push", base, ui_changed, "run=true"),
        ("push", ui_changed, worker_changed, "run=true"),
        ("push", "0" * 40, worker_changed, "run=true"),
    ]
    for event_name, base_sha, head_sha, expected in cases:
        assert (
            _run_detector(
                tmp_path,
                event_name=event_name,
                base_sha=base_sha,
                head_sha=head_sha,
            )
            == expected
        )
