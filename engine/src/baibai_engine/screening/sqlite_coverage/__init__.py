"""Screening SQLite cache coverage verification.

`screening run` refuses to execute unless the cache passes these checks, so
provider fetch gaps surface as explicit issues instead of silent fallbacks.
"""

from .core import verify_screening_sqlite_coverage
from .required_fields import (
    RequiredFieldCoverage,
    RequiredFieldRepairPlan,
    plan_required_field_repair,
    read_required_field_coverage,
)
from .shared import CacheCoverageIssue

__all__ = [
    "CacheCoverageIssue",
    "RequiredFieldCoverage",
    "RequiredFieldRepairPlan",
    "plan_required_field_repair",
    "read_required_field_coverage",
    "verify_screening_sqlite_coverage",
]
