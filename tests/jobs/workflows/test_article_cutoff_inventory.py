"""Exercise the conditional archived-run fact without GitHub or storage access."""

from copy import deepcopy

import pytest
from tools.diagnostics import check_article_cutoff as cutoff

REPO = "koumatsumoto/baibai-loop"
WORKFLOW = 317890420
RUN = 43097164269
RUN_ENDPOINT = f"repos/{REPO}/actions/runs/{RUN}"
JOBS_ENDPOINT = f"{RUN_ENDPOINT}/attempts/1/jobs?per_page=100&page=1"
LIST_ENDPOINT = f"repos/{REPO}/actions/workflows/{WORKFLOW}/runs?per_page=100&page=1"
FLAG = "--report-v2-condition"


@pytest.fixture
def archive(monkeypatch, tmp_path):
    replies = {}
    for number, path in cutoff.LEGACY_WORKFLOWS.items():
        endpoint = f"repos/{REPO}/actions/workflows/{number}"
        replies[endpoint] = {"id": number, "path": path, "state": "disabled_manually"}
        replies[f"{endpoint}/runs?per_page=100&page=1"] = {"total_count": 0, "workflow_runs": []}
    run = {
        "id": RUN,
        "workflow_id": WORKFLOW,
        "path": ".github/workflows/cloud-daily-batch.yml",
        "head_branch": "main",
        "head_sha": "b720659b300e0c8dfbbefa003794bd83265d6261",
        "run_attempt": 1,
        "event": "schedule",
        "status": "queued",
        "conclusion": None,
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
        "created_at": "2026-11-11T20:37:29Z",
        "updated_at": "2026-11-11T20:37:29Z",
    }
    replies[LIST_ENDPOINT] = {"total_count": 1, "workflow_runs": [run]}
    replies[RUN_ENDPOINT] = deepcopy(run)
    replies[JOBS_ENDPOINT] = {"total_count": 0, "jobs": []}
    calls: list[str] = []

    def read(endpoint):
        calls.append(endpoint)
        return replies[endpoint]

    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(cutoff, "github_get", read)
    return replies, calls, output


def test_exact_archived_attempt_reports_only_a_v2_condition(archive, capsys):
    _, calls, output = archive
    assert cutoff.main([FLAG]) == 0
    assert output.read_text() == "requires_v2=true\n"
    assert calls.index(LIST_ENDPOINT) < calls.index(JOBS_ENDPOINT) < calls.index(RUN_ENDPOINT)
    assert "not authorization" in capsys.readouterr().out


def test_unchanged_invocation_still_requires_every_run_to_be_terminal(archive):
    _, calls, output = archive
    assert cutoff.main([]) == 1
    assert not output.exists()
    assert JOBS_ENDPOINT not in calls


@pytest.mark.parametrize("terminal", [True, False])
def test_terminal_or_no_archived_run_does_not_require_v2(archive, terminal):
    replies, calls, output = archive
    replies[LIST_ENDPOINT] = (
        {
            "total_count": 1,
            "workflow_runs": [{"id": RUN, "workflow_id": WORKFLOW, "status": "completed"}],
        }
        if terminal
        else {"total_count": 0, "workflow_runs": []}
    )
    assert cutoff.main([FLAG]) == 0
    assert output.read_text() == "requires_v2=false\n"
    assert JOBS_ENDPOINT not in calls
    assert RUN_ENDPOINT not in calls


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("id", float(RUN)),
        ("workflow_id", float(WORKFLOW)),
        ("head_sha", "a" * 40),
        ("head_branch", "topic"),
        ("path", ".github/workflows/other.yml"),
        ("run_attempt", 2),
        ("run_attempt", True),
        ("status", "in_progress"),
        ("event", "workflow_dispatch"),
        ("conclusion", "success"),
        ("repository", {}),
        ("head_repository", {"full_name": "unrelated/baibai-loop"}),
        ("updated_at", "2026-11-23T01:02:03Z"),
    ],
)
@pytest.mark.parametrize("endpoint", [LIST_ENDPOINT, RUN_ENDPOINT])
def test_either_run_snapshot_must_name_the_exact_unstarted_attempt(archive, key, value, endpoint):
    replies, _, output = archive
    item = replies[endpoint]
    if endpoint == LIST_ENDPOINT:
        [item] = item["workflow_runs"]
    item[key] = value
    assert cutoff.main([FLAG]) == 1
    assert not output.exists()


@pytest.mark.parametrize(
    "listing",
    [
        {"total_count": 1, "jobs": [{"id": 1}]},
        {"total_count": False, "jobs": []},
        {"total_count": 0},
        {"total_count": 0, "jobs": [None]},
        [],
    ],
)
def test_attempt_jobs_must_be_authoritatively_empty(archive, listing):
    replies, _, output = archive
    replies[JOBS_ENDPOINT] = listing
    assert cutoff.main([FLAG]) == 1
    assert not output.exists()


@pytest.mark.parametrize(
    "extra", ["unknown_run", "duplicate", "not_disabled", "missing_fact", "malformed_workflow_id"]
)
def test_other_blockers_prevent_issuing_the_conditional_fact(archive, extra):
    replies, _, output = archive
    if extra == "unknown_run":
        endpoint = f"repos/{REPO}/actions/workflows/401275764/runs?per_page=100&page=1"
        replies[endpoint] = {
            "total_count": 1,
            "workflow_runs": [{"id": 42, "workflow_id": 401275764, "status": "queued"}],
        }
    elif extra == "duplicate":
        [run] = replies[LIST_ENDPOINT]["workflow_runs"]
        replies[LIST_ENDPOINT] = {"total_count": 2, "workflow_runs": [run, run]}
    elif extra == "not_disabled":
        replies[f"repos/{REPO}/actions/workflows/{WORKFLOW}"]["state"] = "active"
    elif extra == "malformed_workflow_id":
        replies[f"repos/{REPO}/actions/workflows/{WORKFLOW}"]["id"] = float(WORKFLOW)
    else:
        del replies[RUN_ENDPOINT]["conclusion"]
    assert cutoff.main([FLAG]) == 1
    assert not output.exists()


def test_unreadable_job_inventory_and_missing_output_fail_closed(archive, monkeypatch):
    _, calls, output = archive
    get = cutoff.github_get

    def denied(endpoint):
        if endpoint == JOBS_ENDPOINT:
            raise OSError("private detail must not be printed")
        return get(endpoint)

    monkeypatch.setattr(cutoff, "github_get", denied)
    assert cutoff.main([FLAG]) == 1
    assert not output.exists()
    before = calls.copy()
    monkeypatch.delenv("GITHUB_OUTPUT")
    assert cutoff.main([FLAG]) == 1
    assert calls == before
