"""Temporary acquisition receipts differ from canonical change-only vintages."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

retain_unchanged = ContextVar("economic_retain_unchanged", default=False)


@contextmanager
def retain_rechecks() -> Iterator[None]:
    """Keep actual received payloads even when an older scoped seed has the same value."""
    token = retain_unchanged.set(True)
    try:
        yield
    finally:
        retain_unchanged.reset(token)
