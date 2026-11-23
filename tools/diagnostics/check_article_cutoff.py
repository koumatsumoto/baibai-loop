"""One-time, read-only proof that pre-reader article/daily workflows cannot run again."""

from __future__ import annotations

import json
import subprocess  # nosec B404
from collections.abc import Callable
from typing import Any

REPO = "koumatsumoto/baibai-loop"
LEGACY_WORKFLOWS = {
    401275764: ".github/workflows/cloud-article-sources.yml",
    317890420: ".github/workflows/cloud-daily-batch.yml",
}
PAGE_SIZE = 100


def _workflow_matches(workflow: Any, identity: int, path: str) -> bool:
    return (
        isinstance(workflow, dict)
        and workflow.get("id") == identity
        and workflow.get("path") == path
    )


def _run_problems(read: Callable[[str], Any], endpoint: str, identity: int) -> list[str]:
    problems = []
    seen: set[int] = set()
    total: int | None = None
    page = 1
    # A run may change from queued to running mid-check. Separate status queries
    # can miss it in both lists; inspect the status on every unfiltered row instead.
    while total is None or len(seen) < total:
        result = read(f"{endpoint}/runs?per_page={PAGE_SIZE}&page={page}")
        if not isinstance(result, dict):
            return [f"{identity}: run inventory not confirmed"]
        count = result.get("total_count")
        runs = result.get("workflow_runs")
        if (
            type(count) is not int
            or count < 0
            or (total is not None and total != count)
            or not isinstance(runs, list)
            or len(runs) != min(PAGE_SIZE, count - len(seen))
        ):
            return [f"{identity}: run inventory changed or is incomplete"]
        total = count
        for run in runs:
            run_id = run.get("id") if isinstance(run, dict) else None
            if type(run_id) is not int or run_id in seen or run.get("workflow_id") != identity:
                return [f"{identity}: run inventory contains unknown or duplicate rows"]
            seen.add(run_id)
            if run.get("status") != "completed":
                problems.append(f"{identity}: run {run_id} is not terminal")
        page += 1
    return problems


def blockers(read: Callable[[str], Any]) -> list[str]:
    """Check the retired workflow IDs, not the names of the current workflows."""
    problems = []
    for identity, path in LEGACY_WORKFLOWS.items():
        endpoint = f"repos/{REPO}/actions/workflows/{identity}"
        workflow = read(endpoint)
        if not _workflow_matches(workflow, identity, path):
            problems.append(f"{identity}: legacy workflow identity not confirmed")
            continue
        if workflow.get("state") != "disabled_manually":
            problems.append(f"{identity}: legacy workflow is not disabled_manually")
        problems.extend(_run_problems(read, endpoint, identity))
        final = read(endpoint)
        if not _workflow_matches(final, identity, path) or final.get("state") != workflow.get(
            "state"
        ):
            problems.append(f"{identity}: legacy workflow state changed during inspection")
    return problems


def github_get(endpoint: str) -> Any:
    # Fixed curl invocation, no shell, configuration file or credentials.
    response = subprocess.run(  # nosec B603 B607
        [
            "curl",
            "--disable",
            "--proto",
            "=https",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "20",
            f"https://api.github.com/{endpoint}",
        ],
        check=True,
        capture_output=True,
        timeout=25,
    )
    return json.loads(response.stdout)


def main() -> int:
    try:
        problems = blockers(github_get)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Article cutoff not confirmed: {type(error).__name__}")
        return 1
    if problems:
        print("Article cutoff blocked:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print("Legacy article/daily workflow fence confirmed; live input readiness is separate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
