"""L3 macro context: publish and show the human-readable environment assessment.

- ``models``: the canonical document contract (core 10 + connection 1)
- ``service``: publication and revision queries against the application DB
- ``cli``: ``baibai-engine macro context``
"""

from __future__ import annotations

from .models import (
    CORE_SECTION_ORDER,
    MACRO_CONTEXT_SCHEMA_VERSION,
    MacroContext,
    MacroContextDocument,
    macro_context_from_payload,
)
from .service import (
    MacroContextConflictError,
    MacroContextNotFoundError,
    MacroContextService,
)

__all__ = [
    "CORE_SECTION_ORDER",
    "MACRO_CONTEXT_SCHEMA_VERSION",
    "MacroContext",
    "MacroContextConflictError",
    "MacroContextDocument",
    "MacroContextNotFoundError",
    "MacroContextService",
    "macro_context_from_payload",
]
