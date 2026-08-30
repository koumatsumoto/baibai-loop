from __future__ import annotations

from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "method/screening/rules/revisions.sha256"


def test_dated_screening_method_revisions_are_immutable() -> None:
    declared: set[Path] = set()
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", maxsplit=1)
        path = ROOT / relative
        declared.add(path)
        assert sha256(path.read_bytes()).hexdigest() == expected

    actual = set((ROOT / "method/screening/rules").glob("*.yaml"))
    assert declared == actual
