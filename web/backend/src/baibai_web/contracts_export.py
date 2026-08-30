"""Generate the read-model contract and the UI types built from it.

`readmodel.models` is the only description of the JSON the UI reads: `materialize`
writes it to `views/*.json` and the local API answers with the same shapes. Restating
that description in TypeScript by hand leaves two files that agree only as long as
somebody remembers, and a backend rename lands in production the next batch while the
UI keeps reading a field the payload no longer carries.

So the TypeScript is generated from the models rather than written beside them, with
the JSON Schema in between checked in as the reviewable artifact. A drift gate
regenerates both and refuses a difference, which turns a rename into a red gate; the
UI's own type check is what then names every place that read the old field.

The emitted schema is a narrow subset — objects, arrays, unions, literals and the
scalar formats Pydantic produces for `date` / `datetime` — so the renderer here reads
that subset exactly and refuses anything outside it rather than guessing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from baibai_web.readmodel.models import (
    CapitalAllocationAssessmentView,
    DailyDeltaView,
    DashboardView,
    MacroContextView,
    MacroReadingView,
    MacroView,
    MetaView,
    OperationsView,
    ScreeningHistoryRunView,
    ScreeningHistoryView,
    ScreeningView,
    SecurityDetailView,
)

SCHEMA_PATH = Path("web/contracts/read-model.schema.json")
TYPES_PATH = Path("web/frontend/src/api/types.ts")
CONTRACT_SCHEMA_VERSION = 1

# Every artifact the UI reads through `/api`, and the model that writes it. The keys are
# the serving paths `web/contracts/routes.json` maps those routes to, and a test pins
# the two against each other so a route added there without a model here is caught.
ROOT_VIEWS: Mapping[str, type[BaseModel]] = {
    "history/candidate-views/{as_of}.json": ScreeningHistoryRunView,
    (
        "views/capital-allocation-assessment--{capital_allocation_assessment_id}.json"
    ): CapitalAllocationAssessmentView,
    "views/daily-delta.json": DailyDeltaView,
    "views/dashboard.json": DashboardView,
    "views/macro--{period}-{granularity}.json": MacroView,
    "views/macro-context--{context_id}.json": MacroContextView,
    "views/macro-reading.json": MacroReadingView,
    "views/meta.json": MetaView,
    "views/operations.json": OperationsView,
    "views/screening_latest.json": ScreeningView,
    "views/security--{ticker}.json": SecurityDetailView,
}

# Routes the Worker answers without a stored artifact. `web/contracts/routes.json`
# calls them computed; they carry the same models and the UI reads them the same way.
# `/api/health` is deliberately absent — it returns a plain dict, not a view model.
COMPUTED_VIEWS: Mapping[str, type[BaseModel]] = {
    "/api/screening/history": ScreeningHistoryView,
}

_GENERATED_HEADER = """\
// Generated from web/backend/src/baibai_web/readmodel/models.py.
// Run `uv run python -m baibai_web.contracts_export` after changing those models;
// `tools/quality/drift/check_readmodel_contract.py` refuses a stale copy.
"""


class ContractError(RuntimeError):
    """The schema holds a construct this renderer does not describe."""


def build_schema() -> dict[str, object]:
    """Emit one JSON Schema document covering every served view."""

    roots = {**ROOT_VIEWS, **COMPUTED_VIEWS}
    _, generated = models_json_schema(
        [(model, "serialization") for model in dict.fromkeys(roots.values())],
        ref_template="#/$defs/{model}",
    )
    definitions = generated.get("$defs", {})
    if not isinstance(definitions, dict):  # pragma: no cover - pydantic always emits a dict
        raise ContractError("pydantic did not emit $defs")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Baibai Loop read model",
        "schemaVersion": CONTRACT_SCHEMA_VERSION,
        "roots": {path: f"#/$defs/{model.__name__}" for path, model in sorted(roots.items())},
        "$defs": {name: _always_written(definitions[name]) for name in sorted(definitions)},
    }


def _always_written(definition: object) -> object:
    """Require every property the writer emits.

    Pydantic marks a field with a default as optional, which describes constructing the
    model rather than the JSON that leaves it: the export writes `model_dump_json` and
    the API answers through `response_model`, and neither omits a field. This schema
    describes the artifact, so a reader can rely on every key being present.

    The one window where that is not true is between a code deploy and the materialize
    that follows it, when the UI reads the previous export. The runbook orders those two
    and the UI degrades on missing sections; a type that called every field optional
    would spread that one window across every field forever.
    """

    if not isinstance(definition, dict):
        return definition
    properties = definition.get("properties")
    if not isinstance(properties, dict):
        return definition
    return {**definition, "required": list(properties)}


def render_schema(schema: Mapping[str, object]) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def _literal_text(value: object) -> str:
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    raise ContractError(f"literal of unsupported type: {value!r}")


def _render_type(node: Mapping[str, object]) -> str:
    if "$ref" in node:
        reference = str(node["$ref"])
        prefix = "#/$defs/"
        if not reference.startswith(prefix):
            raise ContractError(f"unsupported reference: {reference}")
        return reference[len(prefix) :]
    if "const" in node:
        return _literal_text(node["const"])
    if "enum" in node:
        values = node["enum"]
        if not isinstance(values, list) or not values:
            raise ContractError("enum must be a non-empty list")
        return " | ".join(_literal_text(value) for value in values)
    for key in ("anyOf", "oneOf"):
        if key in node:
            options = node[key]
            if not isinstance(options, list) or not options:
                raise ContractError(f"{key} must be a non-empty list")
            members = [_render_type(option) for option in options if isinstance(option, Mapping)]
            if len(members) != len(options):
                raise ContractError(f"{key} members must be objects")
            return " | ".join(dict.fromkeys(members))
    kind = node.get("type")
    if kind == "array":
        items = node.get("items")
        if not isinstance(items, Mapping):
            raise ContractError("array items must be described")
        rendered = _render_type(items)
        return f"Array<{rendered}>" if " | " in rendered else f"{rendered}[]"
    if kind == "object":
        values_schema = node.get("additionalProperties")
        if isinstance(values_schema, Mapping):
            return f"Record<string, {_render_type(values_schema)}>"
        if "properties" not in node:
            return "Record<string, unknown>"
        raise ContractError("inline object properties are not rendered; give the shape a model")
    scalars = {"string": "string", "integer": "number", "number": "number", "boolean": "boolean"}
    if isinstance(kind, str) and kind in scalars:
        return scalars[kind]
    if kind == "null":
        return "null"
    if not node:
        return "unknown"
    raise ContractError(f"unsupported schema node: {json.dumps(node, sort_keys=True)}")


def _doc_comment(text: object, indent: str = "") -> list[str]:
    if not isinstance(text, str) or not text.strip():
        return []
    lines = [line.rstrip() for line in text.strip().splitlines()]
    body = [f"{indent} * {line}".rstrip() for line in lines]
    return [f"{indent}/**", *body, f"{indent} */"]


def render_typescript(schema: Mapping[str, object]) -> str:
    """Render the checked-in TypeScript the UI imports."""

    definitions = schema.get("$defs")
    if not isinstance(definitions, Mapping):
        raise ContractError("schema has no $defs")
    blocks: list[str] = []
    for name in sorted(definitions):
        node = definitions[name]
        if not isinstance(node, Mapping):
            raise ContractError(f"definition {name} is not an object")
        if "properties" not in node:
            blocks.append(
                "\n".join(
                    [
                        *_doc_comment(node.get("description")),
                        f"export type {name} = {_render_type(node)}",
                    ]
                )
            )
            continue
        properties = node["properties"]
        required = node.get("required", [])
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            raise ContractError(f"definition {name} has an unreadable object shape")
        lines = [*_doc_comment(node.get("description")), f"export interface {name} {{"]
        for field, raw in properties.items():
            if not isinstance(raw, Mapping):
                raise ContractError(f"{name}.{field} is not described")
            lines.extend(_doc_comment(raw.get("description"), indent="  "))
            optional = "" if field in required else "?"
            lines.append(f"  {field}{optional}: {_render_type(raw)}")
        lines.append("}")
        blocks.append("\n".join(lines))
    return _GENERATED_HEADER + "\n" + "\n\n".join(blocks) + "\n"


def generated_files(root: Path) -> dict[Path, str]:
    """Every file this module owns, mapped to the text it should hold."""

    schema = build_schema()
    return {
        root / SCHEMA_PATH: render_schema(schema),
        root / TYPES_PATH: render_typescript(schema),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baibai_web.contracts_export",
        description="generate web/contracts/read-model.schema.json and the UI types",
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when a generated file differs instead of writing it",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.repo_root.resolve()
    try:
        files = generated_files(root)
    except ContractError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    stale = [path for path, text in files.items() if _read(path) != text]
    if args.check:
        for path in stale:
            print(f"stale: {path.relative_to(root)}", file=sys.stderr)
        if stale:
            print(
                "run `uv run python -m baibai_web.contracts_export` and commit the result",
                file=sys.stderr,
            )
            return 1
        print(f"read-model contract is current ({len(files)} files)")
        return 0
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(f"wrote {len(files)} files ({len(stale)} changed)")
    return 0


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
