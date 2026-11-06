from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.run_store import (
        RunPublication,
    )

import yaml


class QuotedString(str):
    """A YAML string that should always be quoted."""


class _QuotedDumper(yaml.SafeDumper):
    pass


def _quoted_scalar_representer(dumper: yaml.SafeDumper, data: QuotedString) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')


_QuotedDumper.add_representer(QuotedString, _quoted_scalar_representer)


def published_run_payload(run: RunPublication) -> dict[str, object]:
    """Export the exact saved revision without another rounding or calculation pass."""
    return {
        **run.payload,
        "run_revision_id": run.run_revision_id,
        "stock_analyses": list(run.stock_analyses),
    }


def serialize_published_run_yaml(run: RunPublication) -> str:
    return (
        yaml.dump(
            _quoted_payload(published_run_payload(run)),
            Dumper=_QuotedDumper,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        ).strip()
        + "\n"
    )


def _quoted_payload(value: object) -> object:
    if isinstance(value, str):
        return QuotedString(value)
    if isinstance(value, Mapping):
        return {key: _quoted_payload(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_quoted_payload(item) for item in value]
    return value
