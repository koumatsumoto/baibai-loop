"""JobsのJSON表現とatomic file保存。通知やdomainの意味を持たない。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from baibai_engine.storage.filesystem import write_text_atomic


def canonical_json(payload: object) -> bytes:
    """Keep plain JSON bytes used by Jobs identities, without domain-value coercion."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    """Keep the Jobs JSON representation; the filesystem owner replaces complete bytes."""
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_text_atomic(path, text)
