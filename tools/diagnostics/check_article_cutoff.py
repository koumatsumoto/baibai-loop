"""Read-only workflow retirement evidence, never application write authorization."""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = "koumatsumoto/baibai-loop"
LEGACY_WORKFLOWS = {
    401275764: ".github/workflows/cloud-article-sources.yml",
    317890420: ".github/workflows/cloud-daily-batch.yml",
    394769782: ".github/workflows/cloud-economic-sources.yml",
    394769781: ".github/workflows/cloud-economic-source.yml",
}
PAGE_SIZE = 100

# One unstarted archived attempt whose pinned daily command cannot acquire a v2 lease.
# This is not a GitHub cancellation; the application must still prove a v2 lease.
_ARCHIVED_WORKFLOW = 317890420
_ARCHIVED_RUN = {
    "id": 43097164269,
    "workflow_id": _ARCHIVED_WORKFLOW,
    "path": LEGACY_WORKFLOWS[_ARCHIVED_WORKFLOW],
    "head_branch": "main",
    "head_sha": "b720659b300e0c8dfbbefa003794bd83265d6261",
    "run_attempt": 1,
    "event": "schedule",
    "status": "queued",
    "conclusion": None,
    "created_at": "2026-11-11T20:37:29Z",
    "updated_at": "2026-11-11T20:37:29Z",
}


@dataclass(frozen=True)
class RetirementCheck:
    problems: list[str]
    requires_v2: bool = False


def _workflow_matches(workflow: Any, identity: int, path: str) -> bool:
    return (
        isinstance(workflow, dict)
        and type(workflow.get("id")) is int
        and workflow["id"] == identity
        and workflow.get("path") == path
    )


def _exact_queued_run(run: Any) -> bool:
    return (
        isinstance(run, dict)
        and all(type(run.get(key)) is int for key in ("id", "workflow_id", "run_attempt"))
        and all(key in run and run[key] == value for key, value in _ARCHIVED_RUN.items())
        and all(
            isinstance(repository, dict) and repository.get("full_name") == REPO
            for repository in (run.get("repository"), run.get("head_repository"))
        )
    )


def _archived_has_no_jobs(read: Callable[[str], Any]) -> bool:
    endpoint = f"repos/{REPO}/actions/runs/{_ARCHIVED_RUN['id']}"
    jobs = read(
        f"{endpoint}/attempts/{_ARCHIVED_RUN['run_attempt']}/jobs?per_page={PAGE_SIZE}&page=1"
    )
    return (
        isinstance(jobs, dict)
        and type(jobs.get("total_count")) is int
        and jobs["total_count"] == 0
        and jobs.get("jobs") == []
        # Recheck the latest run after its fixed attempt inventory: it may have started.
        and _exact_queued_run(read(endpoint))
    )


def _run_check(
    read: Callable[[str], Any], endpoint: str, identity: int, *, report_v2_condition: bool
) -> RetirementCheck:
    problems = []
    requires_v2 = False
    seen: set[int] = set()
    total: int | None = None
    page = 1
    # A run may change from queued to running mid-check. Separate status queries
    # can miss it in both lists; inspect the status on every unfiltered row instead.
    while total is None or len(seen) < total:
        result = read(f"{endpoint}/runs?per_page={PAGE_SIZE}&page={page}")
        if not isinstance(result, dict):
            return RetirementCheck([f"{identity}: run inventory not confirmed"])
        count = result.get("total_count")
        runs = result.get("workflow_runs")
        if (
            type(count) is not int
            or count < 0
            or (total is not None and total != count)
            or not isinstance(runs, list)
            or len(runs) != min(PAGE_SIZE, count - len(seen))
        ):
            return RetirementCheck([f"{identity}: run inventory changed or is incomplete"])
        total = count
        for run in runs:
            run_id = run.get("id") if isinstance(run, dict) else None
            if (
                type(run_id) is not int
                or run_id <= 0
                or run_id in seen
                or type(run.get("workflow_id")) is not int
                or run["workflow_id"] != identity
            ):
                return RetirementCheck(
                    [f"{identity}: run inventory contains unknown or duplicate rows"]
                )
            seen.add(run_id)
            if run.get("status") == "completed":
                continue
            if report_v2_condition and _exact_queued_run(run) and _archived_has_no_jobs(read):
                requires_v2 = True
            else:
                problems.append(f"{identity}: run {run_id} is not terminal")
        page += 1
    return RetirementCheck(problems, requires_v2)


def check(read: Callable[[str], Any], *, report_v2_condition: bool = False) -> RetirementCheck:
    """Inspect all four retired IDs and report any separate lease requirement."""
    problems = []
    requires_v2 = False
    for identity, path in LEGACY_WORKFLOWS.items():
        endpoint = f"repos/{REPO}/actions/workflows/{identity}"
        workflow = read(endpoint)
        if not _workflow_matches(workflow, identity, path):
            problems.append(f"{identity}: legacy workflow identity not confirmed")
            continue
        disabled = workflow.get("state") == "disabled_manually"
        if not disabled:
            problems.append(f"{identity}: legacy workflow is not disabled_manually")
        run_check = _run_check(
            read, endpoint, identity, report_v2_condition=report_v2_condition and disabled
        )
        problems.extend(run_check.problems)
        requires_v2 = requires_v2 or run_check.requires_v2
        final = read(endpoint)
        if not _workflow_matches(final, identity, path) or final.get("state") != workflow.get(
            "state"
        ):
            problems.append(f"{identity}: legacy workflow state changed during inspection")
    return RetirementCheck(problems, requires_v2 and not problems)


def github_get(endpoint: str) -> Any:
    # The runner uses its existing read-only token; the operator can use public GET.
    command = (
        ["gh", "api", "--hostname", "github.com", "--method", "GET", endpoint]
        if os.environ.get("GH_TOKEN")
        else [
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
        ]
    )
    response = subprocess.run(  # nosec B603
        command,
        check=True,
        capture_output=True,
        timeout=25,
    )
    return json.loads(response.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-v2-condition", action="store_true")
    args = parser.parse_args(argv)
    output = os.environ.get("GITHUB_OUTPUT") if args.report_v2_condition else None
    if args.report_v2_condition and not output:
        print("Article cutoff not confirmed: condition reporting requires GITHUB_OUTPUT")
        return 1
    try:
        report = check(github_get, report_v2_condition=args.report_v2_condition)
        if report.problems:
            print("Article cutoff blocked:")
            for problem in report.problems:
                print(f"- {problem}")
            return 1
        if output:
            with Path(output).open("a", encoding="utf-8") as stream:
                print(f"requires_v2={str(report.requires_v2).lower()}", file=stream)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Article cutoff not confirmed: {type(error).__name__}")
        return 1
    if report.requires_v2:
        print("Archived queued attempt still exists; this is not authorization without a v2 lease.")
    else:
        print(
            "Legacy article/economic/daily workflow fence confirmed; "
            "live input readiness is separate."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
