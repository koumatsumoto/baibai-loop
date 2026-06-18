"""YAML I/O helpers backed by the C-accelerated loader.

PyYAML's pure-Python ``SafeLoader`` dominates replay wall time (cProfile shows
~93% in scanner/parser for a 6-week sweep). ``CSafeLoader`` is a drop-in
replacement that produces identical output but parses in libyaml. ``yaml.CSafeLoader``
is always present on the build we ship; the fallback is kept only so a future
slim build does not silently break.
"""

from __future__ import annotations

from typing import Any

import yaml

_SafeLoader: type[yaml.SafeLoader] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def safe_load(stream: str | bytes) -> Any:
    """Parse a YAML document with the C-accelerated SafeLoader when available."""
    return yaml.load(stream, Loader=_SafeLoader)
