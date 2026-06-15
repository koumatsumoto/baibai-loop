"""Research-candidate selection: ranking lenses over weekly screening output.

Module map (extension points):

- ``records``: candidate / prior-research record types and loaders
- ``profiles``: built-in profile overrides and ``--profile-config`` resolution
- ``lenses``: per-candidate annotations (fast dislocation, long-hold,
  structural outlook) — add a new lens here and surface it via ``payload``
- ``structural``: AI / structural-decline outlook taxonomy and classifier
- ``ranking``: sort-key components and ``RankingToggles`` (ablation switches)
- ``macro_fit``: macro context fit diagnostics (soft lens, never a gate)
- ``summaries``: output tag / summary rendering
- ``payload``: assembles ranking + diversity + diagnostics into the payload
- ``scorecard``: multi-axis L3 triage shortlist over screen output

The public API below is the stable surface for the CLI, the ledger telemetry
(replay / lane cohorts / ablation), and tests.
"""

from __future__ import annotations

from .payload import build_selection_payload, build_selection_sweep_payload
from .profiles import load_profile_overrides, resolve_selection_rules
from .ranking import RankingToggles
from .records import (
    CandidateRecord,
    PreviousCandidates,
    PriorResearch,
    candidate_record_from_mapping,
    load_previous_candidates,
    load_prior_research,
)
from .scorecard import build_scorecard_payload
from .structural import (
    StructuralOutlook,
    StructuralOutlookConfig,
    classify_structural_outlook,
    load_structural_outlook_config,
)

__all__ = [
    "CandidateRecord",
    "PreviousCandidates",
    "PriorResearch",
    "RankingToggles",
    "StructuralOutlook",
    "StructuralOutlookConfig",
    "build_scorecard_payload",
    "build_selection_payload",
    "build_selection_sweep_payload",
    "candidate_record_from_mapping",
    "classify_structural_outlook",
    "load_previous_candidates",
    "load_prior_research",
    "load_profile_overrides",
    "load_structural_outlook_config",
    "resolve_selection_rules",
]
