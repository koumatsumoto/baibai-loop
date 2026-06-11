"""Research record validation.

Validates research decision records: JSON schema, field vocabularies,
macro-context fit, entry preflight, candidate lineage, position sizing,
payoff arithmetic, and reference integrity.
"""

from .core import (
    discover_research_files,
    load_research_document,
    validate_research_collection,
    validate_research_file,
    validate_research_parsed,
)

__all__ = [
    "discover_research_files",
    "load_research_document",
    "validate_research_collection",
    "validate_research_file",
    "validate_research_parsed",
]
