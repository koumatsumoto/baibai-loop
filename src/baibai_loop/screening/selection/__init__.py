"""Research-candidate selection: ranking lenses over weekly screening output.

Module map (extension points):

- ``records``: candidate / prior-research record types and loaders
- ``profiles``: built-in profile resolution (``balanced`` is the only profile,
  inlined into ``records/_config/screening-rules/*.yaml``)
- ``lenses``: per-candidate annotations (fast dislocation, long-hold) — add a
  new lens here and surface it via ``payload``
- ``ranking``: sort-key components and ``RankingToggles`` (ablation switches)
- ``macro_fit``: macro context fit diagnostics (soft lens, never a gate)
- ``summaries``: output tag / summary rendering
- ``payload``: assembles ranking + diversity + diagnostics into the payload

The public API below is the stable surface for the CLI, the forward telemetry
(replay / playbook cohorts / ablation), and tests.
"""

from __future__ import annotations

from .payload import build_selection_payload, build_selection_sweep_payload
from .profiles import resolve_selection_rules
from .ranking import RankingToggles
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
    "RankingToggles",
    "build_selection_payload",
    "build_selection_sweep_payload",
    "candidate_record_from_mapping",
    "load_previous_candidates",
    "load_prior_research",
    "resolve_selection_rules",
]
