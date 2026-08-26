from __future__ import annotations

import pytest

from baibai_batch.cli import main


def test_job_help_is_delegated_to_the_selected_batch_command(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["daily", "--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "--asof" in output
    assert "--notice-output" in output
