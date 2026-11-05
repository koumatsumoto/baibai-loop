"""作者のYAML入力を読み、評価・Reviewの純粋な契約へ渡す。"""

from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from baibai_engine.investment_judgment.contracts import AssessmentError
from baibai_engine.primitives.yaml import safe_load


def load_draft[T: BaseModel](path: Path, model: type[T], *, label: str) -> T:
    # 契約をI/Oへ依存させず、CLIとworkspaceで同じ入力診断を返す。
    try:
        raw = safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise AssessmentError(f"failed to load {label}: {error}") from error
    if not isinstance(raw, Mapping):
        raise AssessmentError(f"{label} root must be a mapping")
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        raise AssessmentError(str(error)) from error
