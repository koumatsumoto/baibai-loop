"""YAML I/O helpers backed by the C-accelerated loader.

PyYAML's pure-Python ``SafeLoader`` dominates replay wall time (cProfile shows
~93% in scanner/parser for a 6-week sweep). ``CSafeLoader`` is a drop-in
replacement that produces identical output but parses in libyaml. ``yaml.CSafeLoader``
is always present on the build we ship; the fallback is kept only so a future
slim build does not silently break.

All ``src/`` and ``tests/`` readers must import ``safe_load`` from this module
rather than calling ``yaml.safe_load`` directly. Bypassing the helper silently
falls back to the pure-Python loader and regresses replay wall time ~5x.
Callers that need path-aware caching must compose it around this helper rather
than creating a separate YAML loader, so parsing behavior stays identical.
"""

from __future__ import annotations

from typing import Any

import yaml

_SafeLoader: type[yaml.SafeLoader] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def safe_load(stream: str | bytes) -> Any:
    """Parse a YAML document with the C-accelerated SafeLoader when available."""
    # _SafeLoader is yaml.CSafeLoader (preferred) or yaml.SafeLoader (fallback);
    # both restrict construction to YAML's safe subset, so this is not the
    # unsafe yaml.load(stream) the bandit B506 check warns about.
    return yaml.load(stream, Loader=_SafeLoader)  # nosec B506
