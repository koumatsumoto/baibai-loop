"""固定するReading規則の読取。規則の解釈・算術はモデルが所有する。"""

from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from baibai_engine.machine_analysis.models.market_context.reading.rules import (
    ReadingRules,
    ReadingRulesError,
)
from baibai_engine.primitives.yaml import strict_safe_load
from baibai_engine.storage.paths import READING_RULES_PATH

DEFAULT_RULES_PATH = READING_RULES_PATH


def load_reading_rules(path: Path = DEFAULT_RULES_PATH) -> ReadingRules:
    """Load a revision, rejecting a file that names the same series twice.

    A series is overridden for several unrelated reasons (its window, its statistic, its
    publication lag), so a second entry for one series is an easy edit to make. Plain YAML
    would keep the last one and silently drop the settings above it, which reads as a rule
    that was applied.
    """

    try:
        raw = strict_safe_load(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ReadingRulesError(f"invalid Reading rules: {path}: {exc}") from exc
    except OSError as exc:
        raise ReadingRulesError(f"failed to read Reading rules: {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ReadingRulesError(f"Reading rules root must be a mapping: {path}")
    try:
        return ReadingRules.model_validate(raw)
    except ValidationError as exc:
        raise ReadingRulesError(f"invalid Reading rules: {path}: {exc}") from exc


def rules_revision(path: Path = DEFAULT_RULES_PATH) -> str:
    """The revision identity of a rules file: its dated stem."""

    return path.stem
