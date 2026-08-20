"""Overlap lake object fetches with hydrate's verify-and-insert pipeline.

``hydrate_market_store`` consumes objects in one fixed order — dataset by dataset,
partition by partition — and each object passes through ``LakeObjectCache.materialize``,
where its digest is verified and its bytes are installed under the mirror. Read
sequentially, the network wait and the SQLite insert take turns; measured on the
production release, three quarters of a cold fill is that network wait. This module
removes only the taking of turns: workers fetch upcoming objects into a bounded
in-memory buffer, and the cache's read comes out of that buffer instead of the network.

What deliberately does not move off the consuming thread: verification, installation,
and transfer accounting all still happen inside ``materialize``. A worker that returns
damaged bytes is refused by the same digest check that refuses a damaged network read,
and ``fetched_objects`` still counts exactly the objects whose bytes crossed the
network this run.

Failure never widens: any worker-side error marks the remaining plan as skipped, a
skipped key is simply read from the base source on the consuming thread, and a wait
that outlives one fetch by a wide margin walks away to that same base read — the exact
sequential behaviour this module exists to overlap. Prefetching can therefore only be
faster or equal, never a new way for a fill to fail or to hang.

Each worker owns its own source. For R2 that means its own DuckDB session, because a
``LakeSession`` holds one connection and one connection must not serve two threads.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .duck import R2ReadCredentials, lake_session
from .models import LakeObject
from .objects import LakeObjectCache, LakeObjectSource, R2ObjectSource, mirror_path
from .reader import FixedRelease, selected_partitions

_PREFETCH_WORKERS = 8
# Fetched-but-unconsumed payloads the buffer may hold, workers' in-flight fetches
# included. Monthly partition objects run from kilobytes to a few megabytes, so this
# bound is what caps peak memory rather than the object count.
_PREFETCH_BUFFER = 16
# A consumer's wait is one in-flight fetch (the reservation discipline in `_work`
# keeps the key it needs inside the taken window), which completes in seconds. A wait
# this long means the worker is stuck, and the consumer walks away to the base read.
_PREFETCH_WAIT_SECONDS = 120.0
# A worker still alive this long after wind-down is stuck in a read; it is a daemon
# thread over a one-shot CLI, so it is abandoned rather than allowed to hold a fill
# that already completed through the base source.
_PREFETCH_JOIN_SECONDS = 30.0

SourceFactory = Callable[[], AbstractContextManager[LakeObjectSource]]


def hydration_order(release: FixedRelease, dataset_names: Sequence[str]) -> tuple[LakeObject, ...]:
    """The objects of one fill, in exactly the order ``hydrate_market_store`` reads them.

    The fill walks dataset names sorted, partitions in calendar order, and each
    partition's objects in manifest order. The bounded buffer leans on that walk: a
    consumer that read far ahead of plan order could wait on a key the full buffer
    leaves no room to fetch. The two walks agreeing is therefore pinned by a test
    that compares this order against the reads an actual fill performs, so a change
    to either walk fails loudly at development time instead of stalling a run.
    """

    ordered: list[LakeObject] = []
    for name in sorted(set(dataset_names)):
        for partition in selected_partitions(release, name):
            ordered.extend(partition.objects)
    return tuple(ordered)


@dataclass(slots=True)
class _Slot:
    """One planned key's outcome: a payload to hand over, or skipped back to the base."""

    ready: threading.Event = field(default_factory=threading.Event)
    payload: bytes | None = None

    def resolve(self, payload: bytes | None) -> None:
        self.payload = payload
        self.ready.set()


class PrefetchingSource:
    """A ``LakeObjectSource`` whose planned keys are fetched ahead by worker threads.

    Reads of keys outside the plan — and of any planned key the workers skipped —
    fall through to ``base`` unchanged. The consuming thread is the only caller of
    ``read_bytes``; workers only ever resolve slots.
    """

    def __init__(
        self,
        *,
        base: LakeObjectSource,
        objects: Sequence[LakeObject],
        source_factory: SourceFactory,
        mirror: Path,
        workers: int = _PREFETCH_WORKERS,
        buffer_slots: int = _PREFETCH_BUFFER,
        wait_seconds: float = _PREFETCH_WAIT_SECONDS,
    ) -> None:
        if workers < 1 or buffer_slots < workers:
            raise ValueError("prefetch needs at least one worker and one buffer slot each")
        self._base = base
        self._mirror = mirror
        self._wait_seconds = wait_seconds
        self._served = 0
        self._mirrored = 0
        unique: dict[str, LakeObject] = {}
        for item in objects:
            unique.setdefault(item.key, item)
        self._plan: list[tuple[LakeObject, _Slot]] = [(item, _Slot()) for item in unique.values()]
        self._slots: dict[str, _Slot] = {item.key: slot for item, slot in self._plan}
        self._lock = threading.Lock()
        self._next_index = 0
        self._stop = threading.Event()
        self._budget = threading.Semaphore(buffer_slots)
        self._degraded_note = threading.Event()
        self._threads = [
            threading.Thread(
                target=self._work,
                args=(source_factory,),
                name=f"lake-prefetch-{index}",
                daemon=True,
            )
            for index in range(min(workers, len(self._plan)))
        ]
        for thread in self._threads:
            thread.start()

    def read_bytes(self, key: str) -> bytes:
        with self._lock:
            slot = self._slots.pop(key, None)
        if slot is None:
            return self._base.read_bytes(key)
        if not slot.ready.wait(self._wait_seconds):
            # The fetch this key was reserved into never finished. Waiting longer
            # trades a bounded fallback for an unbounded hang, so the prefetch is
            # abandoned and the fill continues on the base read it started from.
            self._note_degraded(f"waited {self._wait_seconds:.0f}s for {key}")
            self._wind_down()
            return self._base.read_bytes(key)
        payload = slot.payload
        if payload is None:
            # Skipped: already mirrored, or the workers wound down. The base source
            # answers, and it is also what reproduces the real error for a key that
            # genuinely cannot be read.
            return self._base.read_bytes(key)
        slot.payload = None
        self._budget.release()
        self._served += 1
        return payload

    def uri(self, key: str) -> str:
        return self._base.uri(key)

    def close(self) -> None:
        """Stop the workers and resolve whatever the plan still holds as skipped.

        The summary line is the read side's one observable: a fill whose step time
        regressed can be told apart as "prefetch degraded" versus "the network or the
        insert got slower" from the run log alone.
        """

        self._wind_down()
        for thread in self._threads:
            thread.join(timeout=_PREFETCH_JOIN_SECONDS)
        if self._plan:
            # The mirrored count keeps a warm fill's "served 0/N" reading as healthy
            # reuse rather than as a silent degrade, which prints its own line.
            print(
                f"lake prefetch served {self._served}/{len(self._plan)} planned objects "
                f"({self._mirrored} already mirrored)",
                file=sys.stderr,
            )

    def _work(self, source_factory: SourceFactory) -> None:
        try:
            with source_factory() as source:
                while True:
                    # The buffer slot is reserved before an item is taken. Taken-but-
                    # unconsumed items are therefore always the next ``buffer_slots``
                    # of the plan, so the key the consumer waits on is always one a
                    # worker already holds — whichever thread a semaphore release
                    # happens to wake. Reserving after taking would let a worker sit
                    # on an early key while later payloads fill the buffer, which
                    # deadlocks the moment the consumer needs that early key.
                    self._budget.acquire()
                    taken = self._take()
                    if taken is None:
                        self._budget.release()
                        return
                    item, slot = taken
                    if not self._fetch(source, item, slot):
                        return
        except Exception as error:  # degrade to the sequential read, never fail the fill
            self._note_degraded(error)
            self._wind_down()

    def _take(self) -> tuple[LakeObject, _Slot] | None:
        with self._lock:
            if self._stop.is_set() or self._next_index >= len(self._plan):
                return None
            taken = self._plan[self._next_index]
            self._next_index += 1
        return taken

    def _fetch(self, source: LakeObjectSource, item: LakeObject, slot: _Slot) -> bool:
        """Resolve one reserved item; False stops this worker after degrading the rest."""

        try:
            if mirror_path(self._mirror, item.key).is_file():
                # Already installed by an earlier run; the cache will reuse it without
                # asking this source, so fetching it again would only be spent bytes.
                self._budget.release()
                with self._lock:
                    self._mirrored += 1
                slot.resolve(None)
                return True
            payload = source.read_bytes(item.key)
        except Exception as error:
            self._budget.release()
            slot.resolve(None)
            self._note_degraded(error)
            self._wind_down()
            return False
        slot.resolve(payload)
        return True

    def _wind_down(self) -> None:
        self._stop.set()
        with self._lock:
            remaining = self._plan[self._next_index :]
            self._next_index = len(self._plan)
        for _, slot in remaining:
            slot.resolve(None)
        # Unblock workers parked on the buffer bound so they can see the stop.
        for _ in self._threads:
            self._budget.release()

    def _note_degraded(self, cause: object) -> None:
        """Say once that reads fall back to sequential, so a slow fill has its cause.

        Messages from the R2 source arrive already redacted, so this cannot carry a
        credential even when the cause is DuckDB's own error.
        """

        if self._degraded_note.is_set():
            return
        self._degraded_note.set()
        print(f"lake prefetch disabled; reads continue sequentially: {cause}", file=sys.stderr)


@contextmanager
def prefetching_hydration_cache(
    cache: LakeObjectCache,
    *,
    release: FixedRelease,
    dataset_names: Sequence[str],
    bucket: str | None,
) -> Iterator[LakeObjectCache]:
    """The cache a fill should read through: prefetched over R2, untouched offline.

    Offline (no bucket) the source is the local mirror itself, where there is no
    latency worth hiding, so the cache passes through unchanged. The returned cache
    shares the original's transfer accounting, so the fill's report keeps describing
    one run whichever cache served it.
    """

    if bucket is None:
        yield cache
        return
    credentials = R2ReadCredentials.from_env(bucket=bucket)

    @contextmanager
    def worker_source() -> Iterator[LakeObjectSource]:
        with lake_session(credentials=credentials) as session:
            yield R2ObjectSource(session)

    source = PrefetchingSource(
        base=cache.source,
        objects=hydration_order(release, dataset_names),
        source_factory=worker_source,
        mirror=cache.root,
    )
    try:
        yield LakeObjectCache(root=cache.root, source=source, transfers=cache.transfers)
    finally:
        source.close()
