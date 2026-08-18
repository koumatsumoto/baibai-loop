from __future__ import annotations

from pathlib import Path

import pytest

from baibai_batch.cli import main as batch_main
from baibai_batch.validation.repository_layout import main as layout_validation_main
from baibai_engine.cli import main as engine_main
from baibai_engine.foundation.repository_layout import (
    StoreLayoutError,
    reject_noncanonical_store_paths,
    repository_root_error,
)
from baibai_engine.research_watch import main as research_watch_main
from baibai_web.cli import main as web_main
from baibai_web.materialize import main as materialize_main


def _repository_root(root: Path) -> Path:
    (root / "pyproject.toml").touch()
    (root / "method").mkdir()
    return root


def _legacy_application_store(root: Path) -> None:
    path = root / "data/app/baibai.sqlite"
    path.parent.mkdir(parents=True)
    path.touch()


def test_legacy_store_path_is_rejected_before_a_second_writer_can_start(tmp_path: Path) -> None:
    _legacy_application_store(_repository_root(tmp_path))
    with pytest.raises(StoreLayoutError, match=r"data/app/baibai\.sqlite"):
        reject_noncanonical_store_paths(tmp_path)


def test_legacy_calibration_directory_is_also_rejected(tmp_path: Path) -> None:
    (_repository_root(tmp_path) / "data/screening/calibration").mkdir(parents=True)
    with pytest.raises(StoreLayoutError, match=r"data/screening/calibration"):
        reject_noncanonical_store_paths(tmp_path)


def test_a_broken_legacy_symlink_is_rejected(tmp_path: Path) -> None:
    path = _repository_root(tmp_path) / "data/app/baibai.sqlite"
    path.parent.mkdir(parents=True)
    path.symlink_to(tmp_path / "missing.sqlite")

    with pytest.raises(StoreLayoutError, match=r"data/app/baibai\.sqlite"):
        reject_noncanonical_store_paths(tmp_path)


@pytest.mark.parametrize(
    "arguments",
    [
        ("--db", "data/app/baibai.sqlite"),
        ("--db=data/app/baibai.sqlite",),
        ("--runs-db", "./data/screening/runs.sqlite"),
    ],
)
def test_a_configured_legacy_path_is_rejected_before_it_exists(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    with pytest.raises(StoreLayoutError, match="exists or is configured"):
        reject_noncanonical_store_paths(_repository_root(tmp_path), raw_arguments=arguments)


def test_the_repository_root_is_established_before_the_layout_is_trusted(tmp_path: Path) -> None:
    # Every store path is root-relative, so scanning a subtree finds no retired path
    # however many the repository holds. Refuse before that clean answer is believed.
    _legacy_application_store(_repository_root(tmp_path))
    subtree = tmp_path / "engine"
    subtree.mkdir()

    with pytest.raises(StoreLayoutError, match="run Baibai Loop from the repository root"):
        reject_noncanonical_store_paths(subtree)


def test_a_repository_root_passes_the_layout_guard(tmp_path: Path) -> None:
    reject_noncanonical_store_paths(_repository_root(tmp_path))


def test_running_from_a_subdirectory_cannot_create_a_second_application_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subtree = _repository_root(tmp_path) / "engine"
    subtree.mkdir()
    monkeypatch.chdir(subtree)

    assert engine_main(["db", "init"]) == 2
    assert not (subtree / "stores").exists()


def test_the_root_report_names_the_marker_the_operator_has_to_fix(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    assert (
        repository_root_error(bare, label="--root")
        == f"--root does not contain pyproject.toml: {bare}"
    )

    (bare / "pyproject.toml").touch()
    assert (
        repository_root_error(bare, label="--repo-root")
        == f"--repo-root does not contain method/: {bare}"
    )

    (bare / "method").mkdir()
    assert repository_root_error(bare, label="--root") is None


def test_a_legacy_environment_override_cannot_recreate_the_old_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_repository_root(tmp_path))
    monkeypatch.setenv("BAIBAI_DB", "data/app/baibai.sqlite")

    assert engine_main(["db", "init"]) == 2
    assert not (tmp_path / "data/app/baibai.sqlite").exists()


def test_an_explicit_legacy_batch_path_is_rejected_before_the_job_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_repository_root(tmp_path))

    assert batch_main(["history-backfill", "--sqlite-path", "data/screening/market.sqlite"]) == 2
    assert not (tmp_path / "data/screening/market.sqlite").exists()


def test_all_runtime_entrypoints_fail_fast_on_a_legacy_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _legacy_application_store(_repository_root(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert engine_main(["db", "info"]) == 2
    assert batch_main(["daily"]) == 2
    assert web_main(["serve", "--root", str(tmp_path)]) == 2
    assert layout_validation_main(["--root", str(tmp_path)]) == 2
    assert (
        materialize_main(["--repo-root", str(tmp_path), "--output-dir", str(tmp_path / "views")])
        == 2
    )
    assert (
        research_watch_main(
            [
                "--db",
                str(tmp_path / "stores/application/baibai.sqlite"),
                "--sqlite-path",
                str(tmp_path / "stores/market/market.sqlite"),
                "--asof",
                "2026-08-08",
            ]
        )
        == 2
    )
    assert capsys.readouterr().err.count("retired store path exists") == 6


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
