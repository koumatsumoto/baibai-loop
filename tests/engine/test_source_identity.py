"""The digest has to move on logic and stay put on presentation.

Both halves are load-bearing. A digest that never moves stops catching an undeclared
change to how a row is computed, which is the only thing the fingerprint exists for. A
digest that moves on a comment invalidates every build under the previous byte for a
change that cannot alter a number — on 2026-08-17 a single docstring line discarded a
46-minute calibration rebuild, and a dependency patch that wrote byte-identical Parquet
stopped the daily batch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from baibai_engine.foundation.source_identity import semantic_source_digest


def _digest(tmp_path: Path, source: str, *, name: str = "module.py") -> str:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return semantic_source_digest(path)


def test_a_comment_does_not_move_the_digest(tmp_path: Path) -> None:
    before = _digest(tmp_path, "def rows(x):\n    return x * 2\n")
    after = _digest(tmp_path, "# explains the doubling\ndef rows(x):\n    return x * 2\n")

    assert before == after


def test_a_docstring_does_not_move_the_digest(tmp_path: Path) -> None:
    before = _digest(tmp_path, "def rows(x):\n    return x * 2\n")
    after = _digest(tmp_path, 'def rows(x):\n    """Double it."""\n    return x * 2\n')

    assert before == after


def test_a_module_docstring_does_not_move_the_digest(tmp_path: Path) -> None:
    before = _digest(tmp_path, "VALUE = 1\n")
    after = _digest(tmp_path, '"""What this module is for."""\n\nVALUE = 1\n')

    assert before == after


def test_reformatting_does_not_move_the_digest(tmp_path: Path) -> None:
    before = _digest(tmp_path, "def rows(x):\n    return x * 2\n")
    after = _digest(tmp_path, "def rows(\n    x,\n):\n\n    return (x\n            * 2)\n")

    assert before == after


def test_a_changed_constant_moves_the_digest(tmp_path: Path) -> None:
    before = _digest(tmp_path, "def rows(x):\n    return x * 2\n")
    after = _digest(tmp_path, "def rows(x):\n    return x * 3\n")

    assert before != after


def test_a_changed_comparison_moves_the_digest(tmp_path: Path) -> None:
    """`<` to `<=` changes which rows a threshold keeps and nothing else."""

    before = _digest(tmp_path, "def keep(x):\n    return x < 5\n")
    after = _digest(tmp_path, "def keep(x):\n    return x <= 5\n")

    assert before != after


def test_a_renamed_local_moves_the_digest(tmp_path: Path) -> None:
    """Names are part of the dump, so this is conservative rather than exact."""

    before = _digest(tmp_path, "def rows(x):\n    total = x * 2\n    return total\n")
    after = _digest(tmp_path, "def rows(x):\n    amount = x * 2\n    return amount\n")

    assert before != after


def test_a_docstring_only_module_still_parses_back(tmp_path: Path) -> None:
    """Removing the only statement would leave a body the grammar rejects."""

    assert _digest(tmp_path, '"""Only a docstring."""\n')


def test_a_file_that_is_not_python_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SyntaxError):
        _digest(tmp_path, "def rows(:\n")


def test_a_patch_release_shares_a_writer_identity_but_a_minor_does_not() -> None:
    """依存の patch は形式を変えず、minor は変えうる。境界をそこへ置く。

    2026-08-17 に pyarrow 25.0.0 から 25.0.1 への bump が L1 の base manifest を全て
    拒否し日次バッチを止めたが、その後の全再構築の created_objects は全 dataset で 0
    だった。書かれた Parquet は 1 バイトも変わっていない。
    """

    from baibai_engine.market.lake.writer import _release_line

    assert _release_line("25.0.0") == _release_line("25.0.1")
    assert _release_line("25.0.0") != _release_line("25.1.0")
    assert _release_line("25.0.0") != _release_line("26.0.0")
    # 版が 1 要素しか無い依存でも壊れない。
    assert _release_line("25") == "25"
