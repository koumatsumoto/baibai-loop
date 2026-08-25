"""Make a moved L1 export identity visible in the pull request that moved it.

The export's `transform_fingerprint` decides whether the daily batch may append to the
published base release. When it moves, the batch stops at publish-lake the next morning
and stays stopped until someone publishes a full rebuild by hand — the guard is working,
but it fires a day late, in the cloud, on a change nobody flagged.

It moves for changes an author reads as inert. Measured over the 119 merges before
2026-08-25, 14 of them (12%) moved it, and the misses cluster in pull requests that are
not about the lake: a docstring line, a pyarrow patch release that wrote byte-identical
Parquet, and the deletion of a dataclass no production path called. The first two were
answered by narrowing what the fingerprint hashes; the third is a real change to the
abstract syntax tree, so narrowing cannot reach it.

So this gate does not narrow anything. It pins the fingerprints beside the tree and fails
when they drift, which turns the invisible consequence into a red gate and a reviewable
diff line while the author can still act. The pin lives here rather than in the engine
because the engine files it covers are themselves hashed into the values — recording them
next to their own definition would move what it records.

Both halves are pinned on purpose. The dataset fingerprints are what production compares,
so they catch everything that can move the published identity: the Arrow schema, the
contract version, the pyarrow release line, the compression settings. The three
implementation digests are what a human edited, so the refusal can name the file that
moved instead of only reporting that something did.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from baibai_engine.foundation.source_identity import semantic_source_digest
from baibai_engine.market.lake.datasets import LAKE_DATASETS

# The private name is used on purpose. Giving `writer.py` a public alias to call from here
# would change `writer.py`, which is hashed into the value this gate pins — installing the
# gate would then require the full rebuild the gate exists to prevent.
from baibai_engine.market.lake.writer import _transform_fingerprint

_PIN_RELATIVE = Path("tools") / "quality" / "drift" / "export_fingerprints.json"

# The three files the export fingerprint hashes, named the way the fingerprint names them.
_IMPLEMENTATION_FILES = (
    "market/lake/datasets.py",
    "market/lake/writer.py",
    "market/sqlite/coverage.py",
)

_RUNBOOK = (
    "この PR は L1 export の identity を変えた。AGENTS.md「lake の export 意味論を変える merge は、"
    "full rebuild release の publish までが 1 つの作業である」に従い、同じ作業の中で "
    "batch/OPERATIONS.md#fingerprint-変更後の-full-rebuild の full rebuild を publish し、"
    "そのうえで `uv run python tools/quality/drift/check_export_fingerprints.py --record` で "
    "pin を更新すること。publish しないまま pin だけ更新すると、翌定時の日次 batch が "
    "publish-lake で停止する。"
)


def _engine_root() -> Path:
    import baibai_engine

    return Path(baibai_engine.__file__).resolve().parent


def current() -> dict[str, dict[str, str]]:
    """The fingerprints this working tree would publish under."""

    engine_root = _engine_root()
    return {
        "datasets": {
            name: _transform_fingerprint(dataset) for name, dataset in sorted(LAKE_DATASETS.items())
        },
        "implementation": {
            name: semantic_source_digest(engine_root / name) for name in _IMPLEMENTATION_FILES
        },
    }


def check(root: Path) -> list[str]:
    pin_path = root / _PIN_RELATIVE
    try:
        recorded = json.loads(pin_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"{_PIN_RELATIVE.name} is missing; run --record to create it"]
    except json.JSONDecodeError as error:
        return [f"{_PIN_RELATIVE.name} is not readable JSON: {error}"]

    actual = current()
    errors: list[str] = []
    for section in ("implementation", "datasets"):
        if not isinstance(recorded.get(section), dict):
            errors.append(f"{_PIN_RELATIVE.name} has no {section!r} section")
    if errors:
        return errors

    # The three implementation digests are the cause a human can act on, so they are
    # reported one per line with the value that moved. The dataset fingerprints are
    # usually the consequence — one edited file moves all of them — so they are named
    # rather than printed as pairs of hashes nobody reads. They still carry their own
    # signal: a change with no implementation line, or one that moves only some datasets,
    # came from the Arrow schema, the contract version or the pyarrow release line.
    for name, was in sorted(recorded["implementation"].items()):
        now = actual["implementation"].get(name)
        if now != was:
            errors.append(
                f"implementation {name}: {was[:16]} -> {'gone' if now is None else now[:16]}"
            )

    pinned, live = recorded["datasets"], actual["datasets"]
    moved = sorted(name for name in pinned.keys() & live.keys() if pinned[name] != live[name])
    if moved:
        errors.append(
            f"datasets: {len(moved)} of {len(live)} fingerprints moved: {', '.join(moved)}"
        )
    if added := sorted(live.keys() - pinned.keys()):
        errors.append(f"datasets: not pinned: {', '.join(added)}")
    if removed := sorted(pinned.keys() - live.keys()):
        errors.append(f"datasets: pinned but gone: {', '.join(removed)}")
    return errors


def record(root: Path) -> Path:
    """Rewrite the pin from this working tree and return the path written."""

    pin_path = root / _PIN_RELATIVE
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    pin_path.write_text(json.dumps(current(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return pin_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--record",
        action="store_true",
        help="rewrite the pin from this working tree (only after publishing a full rebuild)",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[3]
    if args.record:
        print(f"recorded {record(root)}")
        return 0

    errors = check(root)
    for message in errors:
        print(message, file=sys.stderr)
    if errors:
        print(_RUNBOOK, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
