"""Render the frozen Macro World Model into a compact five-part Markdown report."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO, cast

from tools.experiments.macro_world_model.validate_world_model import (
    HORIZONS,
    validate_workspace,
)

from baibai_engine.foundation.yaml_io import safe_load

MAX_NONBLANK_LINES = 120


class ReportRenderError(ValueError):
    """Raised when a validated workspace cannot produce the one-page report."""


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ReportRenderError(f"{label} must be a mapping")
    return cast(Mapping[str, object], value)


def _mapping_list(value: object, *, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ReportRenderError(f"{label} must be a list of mappings")
    return [cast(Mapping[str, object], item) for item in value]


def _strings(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ReportRenderError(f"{label} must be a list of strings")
    return cast(list[str], value)


def _text(row: Mapping[str, object], key: str, *, label: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReportRenderError(f"{label}.{key} is required")
    return value.strip()


def _load_yaml(path: Path) -> Mapping[str, object]:
    if not path.is_file():
        raise ReportRenderError(f"required report input is missing: {path.name}")
    return _mapping(safe_load(path.read_text(encoding="utf-8")), label=path.name)


def _bullets(values: Sequence[str]) -> list[str]:
    return [f"- {value.strip()}" for value in values]


def render_report(workspace: Path, *, freeze_path: Path, revision_diff: Path) -> str:
    """Return a compact Markdown view of validated, hash-frozen inputs."""

    validation = validate_workspace(workspace, freeze_path=freeze_path)
    charter = _load_yaml(workspace / "charter.yaml")
    model = _load_yaml(workspace / "world-model.yaml")
    diff = _load_yaml(revision_diff)

    lines = [
        f"# Macro World Model — {_text(charter, 'as_of', label='charter')}",
        "",
        f"Blind freeze: `{validation['blind_freeze_sha256']}`",
        "",
        "## Key judgments",
        "",
    ]
    judgments = _mapping_list(model.get("key_judgments"), label="world-model.key_judgments")
    lines.extend(
        _bullets(
            [
                f"{_text(row, 'summary', label='key judgment')} "
                f"(horizons: {', '.join(_strings(row.get('horizons'), label='horizons'))})"
                for row in judgments
            ]
        )
    )

    lines.extend(["", "## Horizon paths", ""])
    baseline = _mapping(model.get("baseline_path"), label="world-model.baseline_path")
    for horizon in HORIZONS:
        path = _strings(baseline[horizon], label=f"baseline_path.{horizon}")
        lines.append(f"- **{horizon}:** {'; '.join(item.strip() for item in path)}")

    lines.extend(["", "## Unresolved tensions", ""])
    lines.extend(
        _bullets(
            _strings(model.get("unresolved_tensions"), label="world-model.unresolved_tensions")
        )
    )

    lines.extend(["", "## Signposts", ""])
    lines.extend(_bullets(_strings(model.get("signposts"), label="world-model.signposts")))

    lines.extend(["", "## What changed", ""])
    changes = _mapping_list(diff.get("changes"), label="revision-diff.changes")
    if changes:
        lines.extend(
            _bullets(
                [
                    f"{_text(row, 'area', label='revision diff')}: "
                    f"{_text(row, 'summary', label='revision diff')}"
                    for row in changes
                ]
            )
        )
    else:
        lines.append("- No evidence-backed change is recorded.")

    report = "\n".join(lines) + "\n"
    nonblank_lines = sum(bool(line.strip()) for line in lines)
    if nonblank_lines > MAX_NONBLANK_LINES:
        raise ReportRenderError(
            f"one-page report exceeds {MAX_NONBLANK_LINES} nonblank lines: {nonblank_lines}"
        )
    return report


def _write_new(path: Path, report: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(report)
    except FileExistsError as error:
        raise ReportRenderError(f"refusing to overwrite existing output: {path}") from error


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--freeze", required=True, type=Path)
    parser.add_argument("--revision-diff", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _parse_args(argv)
    output = stdout or sys.stdout
    try:
        report = render_report(
            args.workspace,
            freeze_path=args.freeze,
            revision_diff=args.revision_diff,
        )
        _write_new(args.output, report)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=output)
        return 1
    print(f"rendered {args.output}", file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
