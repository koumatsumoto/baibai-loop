"""Common types for validation findings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Severity = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    """A single issue surfaced by ``baibai-loop-validate``.

    Findings are collected and rendered by the CLI; ``error`` severity drives a
    non-zero exit code so CI can gate merges on schema regressions.
    """

    severity: Severity
    target: Path
    code: str
    message: str
    location: str | None = None
