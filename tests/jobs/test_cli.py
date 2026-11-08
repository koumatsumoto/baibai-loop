from __future__ import annotations

import pytest

from baibai_jobs.cli import main


def test_job_help_is_delegated_to_the_selected_batch_command(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["model-integration", "--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "--asof" in output
    assert "--notice-output" in output


@pytest.mark.parametrize("command", [[], ["collect"], ["import"]])
def test_external_acquisition_help_uses_its_current_jobs_entrypoint(capsys, command) -> None:
    with pytest.raises(SystemExit) as error:
        main(["external-assessment", *command, "--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert output.startswith("usage: baibai-jobs external-assessment")
