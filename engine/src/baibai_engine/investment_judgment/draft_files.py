"""執筆YAMLを一意に読み、判断の型と保存ownerへ渡す。"""

from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from baibai_engine.investment_judgment.contracts import AssessmentError
from baibai_engine.primitives.yaml import strict_safe_load


def parse_authored_yaml(document: str, *, label: str) -> object:
    """Reject conflicting mappings before a writer or reviewer can interpret them."""
    try:
        return strict_safe_load(document)
    except (yaml.YAMLError, ValueError):
        # Parser diagnostics can include draft text or private keys.
        raise ValueError(f"invalid {label} YAML") from None


def read_authored_mapping(path: Path, *, label: str) -> dict[str, object]:
    try:
        raw = parse_authored_yaml(path.read_text(encoding="utf-8"), label=label)
    except (OSError, ValueError) as error:
        raise AssessmentError(f"failed to load {label}: {error}") from error
    if not isinstance(raw, Mapping):
        raise AssessmentError(f"{label} root must be a mapping")
    return dict(raw)


def load_draft[T: BaseModel](path: Path, model: type[T], *, label: str) -> T:
    # 契約をI/Oへ依存させず、CLIとworkspaceで同じ入力診断を返す。
    raw = read_authored_mapping(path, label=label)
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        raise AssessmentError(str(error)) from error
