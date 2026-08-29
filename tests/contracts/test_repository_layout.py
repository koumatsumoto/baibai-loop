from __future__ import annotations

from pathlib import Path

import pytest

from baibai_engine.cli import main as engine_main
from baibai_engine.foundation.repository_layout import (
    StoreLayoutError,
    reject_noncanonical_store_paths,
    repository_root_error,
)


def _repository_root(root: Path) -> Path:
    (root / "pyproject.toml").touch()
    (root / "method").mkdir()
    return root


def test_the_repository_root_is_established_before_the_layout_is_trusted(tmp_path: Path) -> None:
    # Every store path is root-relative, so scanning a subtree finds no retired path
    # however many the repository holds. Refuse before that clean answer is believed.
    _repository_root(tmp_path)
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
