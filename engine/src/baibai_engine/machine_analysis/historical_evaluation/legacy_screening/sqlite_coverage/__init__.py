"""Screening SQLite cache coverage verification.

手動の保守診断として、旧較正契約のsource範囲を確認する。
新しいモデル生成・候補政策へ一律の共通資格として適用しない。
"""

from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.sqlite_coverage.core import (
    verify_screening_sqlite_coverage,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.sqlite_coverage.shared import (
    CacheCoverageIssue,
)

__all__ = [
    "CacheCoverageIssue",
    "verify_screening_sqlite_coverage",
]
