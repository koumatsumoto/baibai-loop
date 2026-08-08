from __future__ import annotations

from pathlib import Path

import pytest

from baibai_batch.cli import main as batch_main
from baibai_engine.cli import main as engine_main
from baibai_engine.foundation.repository_layout import (
    LegacyStorePathError,
    reject_legacy_store_paths,
)
from baibai_web.cli import main as web_main


def _legacy_application_store(root: Path) -> None:
    path = root / "data/app/baibai.sqlite"
    path.parent.mkdir(parents=True)
    path.touch()


def test_legacy_store_path_is_rejected_before_a_second_writer_can_start(tmp_path: Path) -> None:
    _legacy_application_store(tmp_path)
    with pytest.raises(LegacyStorePathError, match=r"data/app/baibai\.sqlite"):
        reject_legacy_store_paths(tmp_path)


def test_legacy_calibration_directory_is_also_rejected(tmp_path: Path) -> None:
    (tmp_path / "data/screening/calibration").mkdir(parents=True)
    with pytest.raises(LegacyStorePathError, match=r"data/screening/calibration"):
        reject_legacy_store_paths(tmp_path)


def test_all_runtime_entrypoints_fail_fast_on_a_legacy_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _legacy_application_store(tmp_path)
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "method").mkdir()
    monkeypatch.chdir(tmp_path)

    assert engine_main(["db", "info"]) == 2
    assert batch_main(["daily"]) == 2
    assert web_main(["serve", "--root", str(tmp_path)]) == 2
    assert capsys.readouterr().err.count("retired store path exists") == 3


def test_public_help_remains_available_while_a_legacy_path_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _legacy_application_store(tmp_path)
    monkeypatch.chdir(tmp_path)

    for help_flag in ("-h", "--help"):
        with pytest.raises(SystemExit) as engine_exit:
            engine_main(["db", help_flag])
        with pytest.raises(SystemExit) as batch_exit:
            batch_main(["daily", help_flag])
        assert engine_exit.value.code == 0
        assert batch_exit.value.code == 0
