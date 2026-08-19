"""The reference index has to keep listing the directory it indexes, both ways."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools.quality.drift import check_reference_index


@pytest.fixture
def directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "thesis.md").write_text("# thesis\n", encoding="utf-8")
    (reference / "macro.md").write_text("# macro\n", encoding="utf-8")
    monkeypatch.setattr(check_reference_index, "_DIRECTORY", reference)
    monkeypatch.setattr(check_reference_index, "_INDEX", reference / "README.md")
    return reference


def _index(directory: Path, body: str) -> None:
    (directory / "README.md").write_text(body, encoding="utf-8")


def test_the_repository_index_passes_its_own_gate() -> None:
    assert check_reference_index.check() == ()


def test_an_index_naming_every_reference_passes(directory: Path) -> None:
    _index(
        directory,
        "| thesis | [`thesis.md`](./thesis.md) |\n| macro | [`macro.md`](./macro.md) |\n",
    )

    assert check_reference_index.check() == ()


def test_a_reference_the_index_omits_is_refused(directory: Path) -> None:
    """The shape that actually occurred: `margin-publication-transition.md` was written,
    the index was not touched, and every gate stayed green until a review read the
    directory instead of the table."""

    _index(directory, "| thesis | [`thesis.md`](./thesis.md) |\n")

    failures = check_reference_index.check()

    assert any("does not index ['macro.md']" in failure for failure in failures)


def test_an_index_row_pointing_at_a_deleted_reference_is_refused(directory: Path) -> None:
    _index(
        directory,
        "| thesis | [`thesis.md`](./thesis.md) |\n"
        "| macro | [`macro.md`](./macro.md) |\n"
        "| gone | [`retired.md`](./retired.md) |\n",
    )

    failures = check_reference_index.check()

    assert any("do not exist: ['retired.md']" in failure for failure in failures)


def test_a_row_pointing_outside_the_directory_is_not_read_as_inventory(directory: Path) -> None:
    """`../architecture.md` indexes another subsystem's contract, not this directory's."""

    _index(
        directory,
        "| thesis | [`thesis.md`](./thesis.md) |\n"
        "| macro | [`macro.md`](./macro.md) |\n"
        "| gates | [`../architecture.md`](../architecture.md) |\n",
    )

    assert check_reference_index.check() == ()


def test_an_unreadable_index_is_refused_rather_than_passed(directory: Path) -> None:
    """A missing index must not read as "nothing to check"."""

    (directory / "README.md").unlink(missing_ok=True)

    failures = check_reference_index.check()

    assert any("unable to read" in failure for failure in failures)
