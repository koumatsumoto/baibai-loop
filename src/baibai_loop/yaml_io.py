"""YAML I/O helpers backed by the C-accelerated loader.

PyYAML's pure-Python ``SafeLoader`` dominates replay wall time (cProfile shows
~93% in scanner/parser for a 6-week sweep). ``CSafeLoader`` is a drop-in
replacement that produces identical output but parses in libyaml. ``yaml.CSafeLoader``
is always present on the build we ship; the fallback is kept only so a future
slim build does not silently break.

All ``src/`` and ``tests/`` readers must import ``safe_load`` from this module
rather than calling ``yaml.safe_load`` directly. Bypassing the helper silently
falls back to the pure-Python loader and regresses replay wall time ~5x. The
single hold-out is ``validate/research/shared.py``, which wraps its loader in
``lru_cache`` keyed by ``(path, mtime_ns, size)`` and needs a path argument the
stream-based ``safe_load`` here does not expose; the same ``getattr`` pattern
is used there so both paths get the C loader.
"""

from __future__ import annotations

from typing import Any

import yaml

_SafeLoader: type[yaml.SafeLoader] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def safe_load(stream: str | bytes) -> Any:
    """Parse a YAML document with the C-accelerated SafeLoader when available."""
    return yaml.load(stream, Loader=_SafeLoader)
