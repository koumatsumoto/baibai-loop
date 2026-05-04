from __future__ import annotations

import os
from pathlib import Path

import pytest

from baibai_loop._env import load_project_env


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("FOO", "BAR"):
        monkeypatch.delenv(key, raising=False)


def test_load_project_env_reads_dotenv_at_start(
    tmp_path: Path, isolated_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("FOO=from_dotenv\n", encoding="utf-8")

    found = load_project_env(tmp_path)

    assert found == tmp_path / ".env"
    assert os.environ["FOO"] == "from_dotenv"


def test_load_project_env_walks_up_to_parent(
    tmp_path: Path, isolated_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("BAR=from_parent\n", encoding="utf-8")
    nested = tmp_path / "sub" / "nested"
    nested.mkdir(parents=True)

    found = load_project_env(nested)

    assert found == tmp_path / ".env"
    assert os.environ["BAR"] == "from_parent"


def test_load_project_env_does_not_override_existing_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOO", "from_shell")
    (tmp_path / ".env").write_text("FOO=from_dotenv\n", encoding="utf-8")

    load_project_env(tmp_path)

    assert os.environ["FOO"] == "from_shell"


def test_load_project_env_returns_none_when_dotenv_missing(
    tmp_path: Path, isolated_env: None
) -> None:
    found = load_project_env(tmp_path)

    assert found is None
