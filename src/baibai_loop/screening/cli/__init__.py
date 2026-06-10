"""Screening CLI package.

The public surface mirrors the former single-module CLI: `main` /
`build_parser` plus the individual command entry points.
"""

from baibai_loop.screening.providers import JQuantsProvider
from baibai_loop.screening.rule_config import load_screening_rules

from .app import build_parser, main
from .cache import (
    bootstrap_cache_command,
    extract_edinet_metrics_command,
    verify_cache_coverage_command,
)
from .providers import ProviderBundle
from .query import (
    market_snapshot_command,
    select_command,
    select_sweep_command,
    ticker_profile_command,
)
from .run import run_command

__all__ = [
    "JQuantsProvider",
    "ProviderBundle",
    "bootstrap_cache_command",
    "build_parser",
    "extract_edinet_metrics_command",
    "load_screening_rules",
    "main",
    "market_snapshot_command",
    "run_command",
    "select_command",
    "select_sweep_command",
    "ticker_profile_command",
    "verify_cache_coverage_command",
]
