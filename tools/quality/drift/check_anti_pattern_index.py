"""Fail CI when AGENTS.md's anti-pattern reference stops covering the catalogue.

`AGENTS.md` sends every commit through the anti-patterns that apply to it. A closed
range written there — "AP-01〜AP-11" — is a claim that goes stale on the day the
twelfth is added, and silently: the new pattern is in the catalogue, the self-review
still reads the old range, and nothing says so. AP-12 and AP-13 lived outside that
range before this gate existed.

So the range is not allowed at all, and the individual ids AGENTS.md does name — the
handful it calls out as "100% 防ぐ" — have to exist. The catalogue itself has to number
without gaps, because "every active anti-pattern" is only a well-defined instruction
while the ids are a sequence.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_AGENTS = _ROOT / "AGENTS.md"
_CATALOGUE = _ROOT / "docs/anti-patterns.md"

_HEADING = re.compile(r"^#+ \d+\. (AP-\d{2}):", re.MULTILINE)
_REFERENCE = re.compile(r"AP-\d{2}")
# Any two ids joined by a range mark, in either the Japanese or the ASCII form.
_RANGE = re.compile(r"AP-\d{2}\s*(?:〜|~|-{1,2}|–|—|to)\s*AP-\d{2}")


def check() -> tuple[str, ...]:
    try:
        agents = _AGENTS.read_text(encoding="utf-8")
        catalogue = _CATALOGUE.read_text(encoding="utf-8")
    except OSError as exc:
        return (f"unable to read the anti-pattern index: {exc}",)

    defined = _HEADING.findall(catalogue)
    failures: list[str] = []
    if not defined:
        return ("docs/anti-patterns.md defines no anti-pattern sections",)

    expected = [f"AP-{index:02d}" for index in range(1, len(defined) + 1)]
    if defined != expected:
        failures.append(
            f"docs/anti-patterns.md ids are not a gapless sequence: {defined} (expected {expected})"
        )
    ranged = _RANGE.search(agents)
    if ranged is not None:
        failures.append(
            f"AGENTS.md names a closed anti-pattern range ({ranged.group(0)}); "
            "reference every active anti-pattern instead, so a new one is covered "
            "the day it is added"
        )
    unknown = sorted(set(_REFERENCE.findall(agents)) - set(defined))
    if unknown:
        failures.append(
            f"AGENTS.md names anti-patterns docs/anti-patterns.md does not define: {unknown}"
        )
    return tuple(failures)


def main() -> int:
    failures = check()
    if failures:
        for failure in failures:
            print(f"error: {failure}")
        return 1
    print("ok: AGENTS.md covers every active anti-pattern")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
