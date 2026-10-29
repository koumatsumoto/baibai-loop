"""Owner-reported outcome of one retained market handoff replay."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReplayResult:
    # Completeness covers this replay, not acquisition coverage or publication.
    report: dict[str, object]
    complete: bool
