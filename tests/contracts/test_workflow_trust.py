from __future__ import annotations

from pathlib import Path

from tools.quality.drift.check_workflow_trust import check, check_workflow


def _workflow(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "workflow.yml"
    path.write_text(body, encoding="utf-8")
    return path


def test_repository_workflows_keep_generic_trust_boundaries() -> None:
    assert check(Path.cwd()) == []


def test_external_action_requires_a_reviewed_full_sha(tmp_path: Path) -> None:
    path = _workflow(tmp_path, "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v7\n")

    assert any("full commit SHA" in error for error in check_workflow(path))


def test_dispatch_input_cannot_be_interpolated_in_shell(tmp_path: Path) -> None:
    path = _workflow(
        tmp_path,
        "jobs:\n  test:\n    steps:\n      - run: echo '${{ inputs.value }}'\n",
    )

    assert any("step env" in error for error in check_workflow(path))


def test_credential_cannot_be_scoped_to_a_whole_job(tmp_path: Path) -> None:
    path = _workflow(
        tmp_path,
        "jobs:\n  test:\n    env:\n      TOKEN: ${{ secrets.TOKEN }}\n    steps:\n      - run: true\n",
    )

    assert any("one step env" in error for error in check_workflow(path))
