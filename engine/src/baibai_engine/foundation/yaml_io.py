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


def _construct_unique_mapping(
    loader: yaml.SafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise ValueError(f"unhashable YAML mapping key: {key!r}") from error
        if duplicate:
            raise ValueError(f"duplicate YAML mapping key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader: type[yaml.SafeLoader] = type("_StrictSafeLoader", (_SafeLoader,), {})
_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def safe_load(stream: str | bytes) -> Any:
    """Parse a YAML document with the C-accelerated SafeLoader when available."""
    # _SafeLoader is yaml.CSafeLoader (preferred) or yaml.SafeLoader (fallback);
    # both restrict construction to YAML's safe subset, so this is not the
    # unsafe yaml.load(stream) the bandit B506 check warns about.
    return yaml.load(stream, Loader=_SafeLoader)  # nosec B506


def strict_safe_load(stream: str | bytes) -> Any:
    """Parse safe YAML and reject duplicate mapping keys."""
    return yaml.load(stream, Loader=_StrictSafeLoader)  # nosec B506
