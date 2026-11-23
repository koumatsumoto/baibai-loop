import pytest
from tools.diagnostics import check_article_cutoff as cutoff


def _snapshot():
    replies = {}
    for identity, path in cutoff.LEGACY_WORKFLOWS.items():
        endpoint = f"repos/{cutoff.REPO}/actions/workflows/{identity}"
        replies[endpoint] = {"id": identity, "path": path, "state": "disabled_manually"}
        replies[f"{endpoint}/runs?per_page=100&page=1"] = {
            "total_count": 0,
            "workflow_runs": [],
        }
    return replies


def test_only_disabled_empty_legacy_ids_confirm_workflow_fence():
    replies = _snapshot()
    assert cutoff.blockers(replies.__getitem__) == []


def test_active_legacy_workflow_blocks_even_when_currently_empty():
    replies = _snapshot()
    endpoint = f"repos/{cutoff.REPO}/actions/workflows/401275764"
    replies[endpoint]["state"] = "active"
    assert cutoff.blockers(replies.__getitem__) == [
        "401275764: legacy workflow is not disabled_manually"
    ]


@pytest.mark.parametrize(
    "status", ["requested", "queued", "pending", "in_progress", "waiting", "unknown"]
)
def test_disabling_workflow_never_treats_old_runs_as_cancelled(status):
    replies = _snapshot()
    endpoint = f"repos/{cutoff.REPO}/actions/workflows/317890420/runs?per_page=100&page=1"
    replies[endpoint] = {
        "total_count": 1,
        "workflow_runs": [{"id": 43097164269, "workflow_id": 317890420, "status": status}],
    }
    assert cutoff.blockers(replies.__getitem__) == ["317890420: run 43097164269 is not terminal"]


def test_absent_status_or_different_workflow_path_cannot_verify_retirement():
    replies = _snapshot()
    article = f"repos/{cutoff.REPO}/actions/workflows/401275764"
    daily = f"repos/{cutoff.REPO}/actions/workflows/317890420"
    replies[article]["path"] = ".github/workflows/unrelated.yml"
    replies[f"{daily}/runs?per_page=100&page=1"] = {"total_count": False}
    assert cutoff.blockers(replies.__getitem__) == [
        "401275764: legacy workflow identity not confirmed",
        "317890420: run inventory changed or is incomplete",
    ]


@pytest.mark.parametrize("final_id", [43097164269, 1])
def test_entire_snapshot_includes_old_queued_runs_and_rejects_duplicate_pages(final_id):
    replies = _snapshot()
    endpoint = f"repos/{cutoff.REPO}/actions/workflows/317890420/runs?per_page=100"
    replies[f"{endpoint}&page=1"] = {
        "total_count": 101,
        "workflow_runs": [
            {"id": number, "workflow_id": 317890420, "status": "completed"}
            for number in range(1, 101)
        ],
    }
    replies[f"{endpoint}&page=2"] = {
        "total_count": 101,
        "workflow_runs": [{"id": final_id, "workflow_id": 317890420, "status": "queued"}],
    }
    assert cutoff.blockers(replies.__getitem__) == [
        "317890420: run 43097164269 is not terminal"
        if final_id == 43097164269
        else "317890420: run inventory contains unknown or duplicate rows"
    ]


def test_state_is_rechecked_after_reading_the_run_inventory():
    replies = _snapshot()
    endpoint = f"repos/{cutoff.REPO}/actions/workflows/317890420"
    states = iter(["disabled_manually", "active"])

    def read(path):
        if path == endpoint:
            return {**replies[path], "state": next(states)}
        return replies[path]

    assert cutoff.blockers(read) == ["317890420: legacy workflow state changed during inspection"]
