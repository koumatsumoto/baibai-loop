"""Thesis domain engine.

Validates thesis decision records (investment memos): JSON schema, field
vocabularies, entry preflight, position sizing,
payoff arithmetic, and reference integrity. Consumes ``screening.regime``
and ``position.policy`` to gate and size theses, so it sits above both as a
top-level subsystem rather than under either.
"""

from .core import (
    discover_thesis_files,
    load_thesis_document,
    validate_thesis_collection,
    validate_thesis_file,
    validate_thesis_parsed,
)

__all__ = [
    "discover_thesis_files",
    "load_thesis_document",
    "validate_thesis_collection",
    "validate_thesis_file",
    "validate_thesis_parsed",
]
