"""Screening SQLite cache coverage verification.

手動の保守診断として、旧較正契約のsource範囲を確認する。
新しいモデル生成・候補政策へ一律の共通資格として適用しない。
"""

from .core import verify_screening_sqlite_coverage
from .shared import CacheCoverageIssue

__all__ = [
    "CacheCoverageIssue",
    "verify_screening_sqlite_coverage",
]
