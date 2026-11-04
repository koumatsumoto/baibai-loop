"""Read pending J-Quants original pages without loading or projecting L2."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsSource, RetainedRequest
from baibai_engine.data_collection.providers.jquants.retained import decode_retained_rows
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.acquisition.receipts import ReceiptStore, available_handoffs, download_fixed

if TYPE_CHECKING:
    from baibai_engine.storage.snapshots.reader import FixedRelease


def iter_pending_requests(
    path: Path, *, source: JQuantsSource, known_at: datetime
) -> Iterator[RetainedRequest]:
    """Read one verified private handoff; caller owns its retention and digest check."""
    if known_at.utcoffset() is None:
        raise ValueError("known_at requires a timezone")
    if not path.is_absolute() or not path.is_file():
        raise ValueError("verified local handoff is required")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            "SELECT request_id, page_index, source, query_json, received_at_utc, body_gzip "
            "FROM jquants_source_pages WHERE request_id IN "
            "(SELECT request_id FROM jquants_source_pages WHERE source = ?) "
            "ORDER BY request_id, page_index",
            (source,),
        )
        current: str | None = None
        pages: list[tuple[object, ...]] = []
        for row in rows:
            identity = str(row[0])
            if current is not None and identity != current:
                if pages:
                    yield decode_retained_rows(current, pages)
                pages = []
            current = identity
            try:
                received = datetime.fromisoformat(str(row[4]))
                if received.utcoffset() is None:
                    raise ValueError("receipt time requires a timezone")
            except ValueError:
                # Keep the malformed page so it becomes an explicit incomplete receipt.
                pages.append(row)
            else:
                if received <= known_at:
                    pages.append(row)
        if current is not None and pages:
            yield decode_retained_rows(current, pages)


def retained_snapshot_time(release: FixedRelease) -> datetime | None:
    """Earliest source snapshot in the fixed L1 dataset; unknown means scan all."""
    manifest = release.dataset_manifest("jquants.source_pages")
    if any(not part.sources for part in manifest.partitions):
        return None
    sources = [source for part in manifest.partitions for source in part.sources]
    return min((source.captured_at for source in sources), default=None)


def select_handoffs_after_snapshot(
    entries: list[dict[str, object]],
    *,
    captured_at: datetime | None,
    source: str = "jquants-financial",
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    """Keep originals not provably in the fixed snapshot; never consult L2."""
    selected: list[dict[str, object]] = []
    unavailable: list[str] = []
    for entry in entries:
        stage = str(entry.get("stage_id", ""))
        if entry.get("invalid"):
            unavailable.append(stage)
            continue
        if entry.get("source") != source:
            raise ValueError("unexpected handoff source")
        marker = entry.get("integration")
        if isinstance(marker, dict) and marker.get("status") == "integrated" and captured_at:
            try:
                integrated_at = datetime.fromisoformat(str(marker["integrated_at"]))
                if integrated_at.utcoffset() is None:
                    raise ValueError("integration time requires a timezone")
            except (KeyError, ValueError):
                pass
            else:
                if integrated_at < captured_at:
                    continue
        selected.append(entry)
    return selected, tuple(sorted(unavailable))


@contextmanager
def open_pending_originals(
    store: ReceiptStore,
    *,
    release: FixedRelease,
    source: str,
    clock: Callable[[], datetime],
) -> Iterator[tuple[datetime, tuple[tuple[str, Path], ...], tuple[str, ...]]]:
    """Freeze and verify only originals not yet provable in the L1 snapshot."""
    entries = available_handoffs(store, source=source, include_integrated=True)
    known_at = clock()
    if known_at.utcoffset() is None:
        raise ValueError("clock requires a timezone")
    selected, invalid = select_handoffs_after_snapshot(
        entries, captured_at=retained_snapshot_time(release), source=source
    )
    missing = list(invalid)
    with TemporaryDirectory(prefix="jquants-pending-") as temporary:
        paths: list[tuple[str, Path]] = []
        for number, entry in enumerate(selected):
            directory = Path(temporary) / str(number)
            try:
                download_fixed(store, entry, directory)
            except (LakePublishError, OSError, ValueError):
                missing.append(str(entry["stage_id"]))
            else:
                paths.append((str(entry["stage_id"]), directory / "receipts.sqlite"))
        yield known_at, tuple(paths), tuple(sorted(missing))
