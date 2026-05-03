"""Helpers for migrating the legacy `.cache/screening/` raw JSON tree to
`data/raw/screening/`.

Issue #45 moved the raw JSON cache from a `.gitignore`d local workspace to a
git-tracked location so that another machine can rebuild screening / ledger
inputs from a fresh `git clone`. Existing checkouts have files at the old
path; this module performs the one-time move without re-downloading from
J-Quants / EDINET / JPX.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MigrationPlanEntry:
    source: Path
    destination: Path
    size: int


@dataclass(frozen=True, slots=True)
class MigrationResult:
    moved: tuple[MigrationPlanEntry, ...]
    skipped: tuple[MigrationPlanEntry, ...]

    @property
    def moved_bytes(self) -> int:
        return sum(entry.size for entry in self.moved)

    @property
    def skipped_bytes(self) -> int:
        return sum(entry.size for entry in self.skipped)


def plan_migration(source_root: Path, destination_root: Path) -> tuple[MigrationPlanEntry, ...]:
    """Build the migration plan without moving any files."""
    if not source_root.exists():
        return ()
    return tuple(_iter_plan(source_root, destination_root))


def migrate_cache(
    source_root: Path,
    destination_root: Path,
    *,
    dry_run: bool = False,
) -> MigrationResult:
    """Move every file under `source_root` to the matching path under
    `destination_root`, preserving relative paths.

    A destination that already exists is skipped (not overwritten) so a
    re-run after a partial migration stays a no-op for already-moved files.
    The source tree is left in place; empty directories are removed at the
    end so a follow-up `rmdir .cache/screening` works.
    """
    plan = plan_migration(source_root, destination_root)
    moved: list[MigrationPlanEntry] = []
    skipped: list[MigrationPlanEntry] = []

    for entry in plan:
        if entry.destination.exists():
            skipped.append(entry)
            continue
        if dry_run:
            moved.append(entry)
            continue
        entry.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry.source), str(entry.destination))
        moved.append(entry)

    if not dry_run:
        _prune_empty_dirs(source_root)

    return MigrationResult(moved=tuple(moved), skipped=tuple(skipped))


def _iter_plan(source_root: Path, destination_root: Path) -> Iterator[MigrationPlanEntry]:
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source_root)
        yield MigrationPlanEntry(
            source=path,
            destination=destination_root / relative,
            size=path.stat().st_size,
        )


def _prune_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                # Directory still has remaining files (e.g. manifests/) — keep it.
                continue
    try:
        root.rmdir()
    except OSError:
        return
