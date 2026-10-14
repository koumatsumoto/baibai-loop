from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from baibai_web.materialize import _write_history


def test_indexed_history_missing_from_lookup_aborts_export(tmp_path: Path, mocker) -> None:
    expected_date = date(2026, 10, 14)
    mocker.patch("baibai_web.materialize.screening_run_asof_dates", return_value=[expected_date])
    mocker.patch("baibai_web.materialize.build_screening_history_run", return_value=None)

    with pytest.raises(ValueError, match="indexed Screening history is unavailable: 2026-10-14"):
        _write_history(
            tmp_path,
            screening_source=mocker.Mock(),
            ledger=mocker.Mock(),
            research=mocker.Mock(),
            runs_db_path=tmp_path / "runs.sqlite",
        )

    assert not (tmp_path / "history").exists()
