"""Research-candidate selection: valuation-discount ranking over screening output.

Module map (extension points):

- ``records``: candidate / prior-research record types and loaders
- ``profiles``: built-in profile resolution (``balanced`` is the only profile,
  inlined into ``records/_config/screening-rules/*.yaml``)
- ``lenses``: per-candidate annotations (durability / 塩漬け耐性) — add a
  new lens here and surface it via ``payload``
- ``ranking``: sort-key components (playbook order + valuation-discount strength)
- ``macro_fit``: macro context fit diagnostics (soft lens, never a gate)
- ``summaries``: output tag / summary rendering
- ``payload``: assembles ranking + diversity + diagnostics into the payload

The public API below is the stable surface for the CLI and tests.
"""

from __future__ import annotations

from .payload import build_selection_payload, build_selection_sweep_payload
from .profiles import resolve_selection_rules
from .records import (
    CandidateRecord,
    PreviousCandidates,
    PriorResearch,
    candidate_record_from_mapping,
    load_previous_candidates,
    load_prior_research,
)

__all__ = [
    "CandidateRecord",
    "PreviousCandidates",
    "PriorResearch",
    "build_selection_payload",
    "build_selection_sweep_payload",
    "candidate_record_from_mapping",
    "load_previous_candidates",
    "load_prior_research",
    "resolve_selection_rules",
]
