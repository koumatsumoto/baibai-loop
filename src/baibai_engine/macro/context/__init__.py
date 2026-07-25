"""L3 macro context: the published environment assessment and its consumption.

- ``models``: the canonical document contract (core 10 + connection 1)
- ``service``: publication and revision queries against the application DB
- ``diagnostics``: freshness policy and payload accessors for consumers
- ``cli``: ``baibai-engine macro context``
"""

from __future__ import annotations

from .diagnostics import (
    MACRO_CONTEXT_STALE_DAYS,
    MacroContext,
    macro_context_diagnostics,
    macro_context_from_payload,
)
from .models import (
    CORE_SECTION_ORDER,
    MACRO_CONTEXT_SCHEMA_VERSION,
    MacroContextDocument,
)
from .service import (
    MacroContextConflictError,
    MacroContextNotFoundError,
    MacroContextService,
)

__all__ = [
    "CORE_SECTION_ORDER",
    "MACRO_CONTEXT_SCHEMA_VERSION",
    "MACRO_CONTEXT_STALE_DAYS",
    "MacroContext",
    "MacroContextConflictError",
    "MacroContextDocument",
    "MacroContextNotFoundError",
    "MacroContextService",
    "macro_context_diagnostics",
    "macro_context_from_payload",
]
