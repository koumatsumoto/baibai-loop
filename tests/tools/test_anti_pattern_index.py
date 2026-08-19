"""The self-review index has to keep covering the catalogue it points at."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools.quality.drift import check_anti_pattern_index


@pytest.fixture
def catalogue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    catalogue_path = docs / "anti-patterns.md"
    catalogue_path.write_text(
        "## 1. AP-01: first\n\n## 2. AP-02: second\n\n## 3. AP-03: third\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_anti_pattern_index, "_ROOT", tmp_path)
    monkeypatch.setattr(check_anti_pattern_index, "_CATALOGUE", catalogue_path)
    monkeypatch.setattr(check_anti_pattern_index, "_AGENTS", tmp_path / "AGENTS.md")
    return catalogue_path


def _agents(tmp_path: Path, body: str) -> None:
    (tmp_path / "AGENTS.md").write_text(body, encoding="utf-8")


def test_the_repository_index_passes_its_own_gate() -> None:
    assert check_anti_pattern_index.check() == ()


def test_a_closed_range_is_refused_however_it_is_written(tmp_path: Path, catalogue: Path) -> None:
    """A range is a claim that goes stale on the day the next pattern is added, and
    silently — which is how AP-12 and AP-13 came to sit outside the one AGENTS.md named.
    """
    del catalogue

    for written in ("AP-01〜AP-03", "AP-01~AP-03", "AP-01-AP-03", "AP-01 to AP-03"):
        _agents(tmp_path, f"pass the checklists for {written}.\n")

        failures = check_anti_pattern_index.check()

        assert any("closed anti-pattern range" in failure for failure in failures), written


def test_naming_every_active_pattern_passes(tmp_path: Path, catalogue: Path) -> None:
    del catalogue
    _agents(tmp_path, "全 active anti-pattern を通す。特に AP-02 は 100% 防ぐ。\n")

    assert check_anti_pattern_index.check() == ()


def test_naming_a_pattern_the_catalogue_dropped_is_refused(tmp_path: Path, catalogue: Path) -> None:
    del catalogue
    _agents(tmp_path, "全 active anti-pattern を通す。特に AP-09 は 100% 防ぐ。\n")

    failures = check_anti_pattern_index.check()

    assert any("does not define" in failure for failure in failures)


def test_a_gap_in_the_catalogue_is_refused(tmp_path: Path, catalogue: Path) -> None:
    """ "Every active anti-pattern" is only a well-defined instruction while the ids are
    a sequence: a gap means a reader cannot tell a retired one from a missing one."""

    catalogue.write_text("## 1. AP-01: first\n\n## 2. AP-03: third\n", encoding="utf-8")
    _agents(tmp_path, "全 active anti-pattern を通す。\n")

    failures = check_anti_pattern_index.check()

    assert any("gapless sequence" in failure for failure in failures)
