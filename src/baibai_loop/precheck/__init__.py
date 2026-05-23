"""Mechanized self-review checks for anti-patterns."""

from .decision_flip import DecisionFlipFinding, scan_research_decision_flips

__all__ = [
    "DecisionFlipFinding",
    "scan_research_decision_flips",
]
