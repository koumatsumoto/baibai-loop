"""Check generic GitHub Actions execution and credential boundaries."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import yaml

_ACTION = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?P<owner>[^/@\s]+/[^@\s]+)@(?P<sha>[0-9a-f]{40})"
    r"(?:\s+#\s*(?P<release>\S+))?\s*$"
)
_KNOWN_ACTIONS = {
    "actions/cache": ("55cc8345863c7cc4c66a329aec7e433d2d1c52a9", "v6.1.0"),
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    "actions/setup-node": ("820762786026740c76f36085b0efc47a31fe5020", "v7.0.0"),
    "actions/setup-python": ("5fda3b95a4ea91299a34e894583c3862153e4b97", "v7.0.0"),
    "astral-sh/setup-uv": ("20cfd1bf945f4377ade1205e4dbc17946fc9a30d", "v10.0.1"),
}
_INPUT = re.compile(r"\$\{\{[^}]*\binputs\b[^}]*\}\}")
_CREDENTIAL = re.compile(r"\$\{\{[^}]*(?:\bsecrets\b|\bvars\.R2_ACCOUNT_ID\b)[^}]*\}\}")

type PathPart = str | int
type DocumentPath = tuple[PathPart, ...]


def _mapping(value: object) -> Mapping[object, object] | None:
    return value if isinstance(value, Mapping) else None


def _iter_scalars(value: object, path: DocumentPath = ()) -> Iterator[tuple[DocumentPath, str]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _iter_scalars(child, (*path, str(key)))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for index, child in enumerate(value):
            yield from _iter_scalars(child, (*path, index))
    elif isinstance(value, str):
        yield path, value


def _display(path: DocumentPath) -> str:
    return ".".join(f"[{part}]" if isinstance(part, int) else part for part in path)


def _action_errors(path: Path, text: str) -> list[str]:
    errors: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if "uses:" not in line or line.lstrip().startswith("#"):
            continue
        reference = line.split("uses:", 1)[1].strip()
        if reference.startswith("./"):
            continue
        match = _ACTION.match(line)
        if match is None:
            errors.append(f"{path.name}:{number}: external action must use a full commit SHA")
            continue
        action = match.group("owner")
        expected = _KNOWN_ACTIONS.get(action)
        if expected is None:
            errors.append(f"{path.name}:{number}: external action is not reviewed: {action}")
        elif (match.group("sha"), match.group("release")) != expected:
            errors.append(f"{path.name}:{number}: action SHA and release comment are not reviewed")
    return errors


def check_workflow(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    try:
        loaded: object = yaml.load(text, Loader=yaml.BaseLoader)  # nosec B506
    except yaml.YAMLError as error:
        return [f"{path.name}: invalid workflow YAML: {error}"]
    workflow = _mapping(loaded)
    if workflow is None:
        return [f"{path.name}: workflow must be a mapping"]

    errors = _action_errors(path, text)
    for location, value in _iter_scalars(workflow):
        if _INPUT.search(value) and location[-1:] == ("run",):
            errors.append(
                f"{path.name}:{_display(location)}: dispatch input must pass through step env"
            )
        if _CREDENTIAL.search(value):
            in_step_env = (
                len(location) >= 6
                and location[0] == "jobs"
                and location[2] == "steps"
                and isinstance(location[3], int)
                and location[4] == "env"
            )
            if not in_step_env:
                errors.append(
                    f"{path.name}:{_display(location)}: credential must be scoped to one step env"
                )
    return errors


def check(root: Path) -> list[str]:
    workflow_dir = root / ".github" / "workflows"
    paths = sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")))
    return [error for path in paths for error in check_workflow(path)]


def main() -> int:
    errors = check(Path.cwd())
    print("\n".join(errors), file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
