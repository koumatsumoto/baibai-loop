"""The generated read-model contract has to describe the JSON that is actually served."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from baibai_web.contracts_export import (
    COMPUTED_VIEWS,
    ROOT_VIEWS,
    ContractError,
    build_schema,
    render_typescript,
)
from baibai_web.materialize import export_read_models

ROOT = Path(__file__).resolve().parents[2]
ROUTES = json.loads((ROOT / "web/contracts/routes.json").read_text(encoding="utf-8"))


def test_every_route_the_worker_serves_has_a_model_in_the_contract() -> None:
    """A view added to the routes without a model here would be served with no type.

    The two files answer the same question from different ends — which artifact backs
    which route, and which model writes which artifact — so they are pinned against
    each other rather than each maintained on its own.
    """

    served = {
        *ROUTES["routes"].values(),
        *ROUTES["patterns"].values(),
    }
    computed = set(ROUTES["computed_routes"]) - {"/api/health"}

    assert set(ROOT_VIEWS) == served
    assert set(COMPUTED_VIEWS) == computed
    # `/api/health` returns a plain dict, so it does not belong to the read models.
    # Naming it keeps a future route from being dropped silently by widening the
    # exclusion.
    assert "/api/health" in ROUTES["computed_routes"]
    assert ROUTES["edge_routes"] == {}


def _definition(schema: Mapping[str, object], reference: str) -> Mapping[str, object]:
    definitions = schema["$defs"]
    assert isinstance(definitions, Mapping)
    node = definitions[reference.removeprefix("#/$defs/")]
    assert isinstance(node, Mapping)
    return node


def _check_value(
    schema: Mapping[str, object], node: Mapping[str, object], value: object, path: str
) -> list[str]:
    """Report every place the payload and the schema disagree about which keys exist."""

    if "$ref" in node:
        return _check_value(schema, _definition(schema, str(node["$ref"])), value, path)
    for key in ("anyOf", "oneOf"):
        options = node.get(key)
        if isinstance(options, Sequence) and not isinstance(options, (str, bytes)):
            # A union is satisfied by any member; the payload picks one at runtime, so
            # only a member that matches the value's shape can be checked against it.
            failures = [
                _check_value(schema, option, value, path)
                for option in options
                if isinstance(option, Mapping)
            ]
            return [] if any(not item for item in failures) else min(failures, key=len)
    if node.get("type") == "array" and isinstance(value, list):
        items = node.get("items")
        if not isinstance(items, Mapping):
            return []
        return [
            failure
            for index, item in enumerate(value)
            for failure in _check_value(schema, items, item, f"{path}[{index}]")
        ]
    properties = node.get("properties")
    if not isinstance(properties, Mapping):
        return []
    if not isinstance(value, Mapping):
        return [f"{path}: schema describes an object, payload holds {type(value).__name__}"]
    required = node.get("required")
    assert isinstance(required, list)
    missing = [name for name in required if name not in value]
    extra = [name for name in value if name not in properties]
    failures = [f"{path}: payload omits {name}" for name in missing]
    failures += [f"{path}: payload carries undescribed {name}" for name in extra]
    for name, child in properties.items():
        if name in value and isinstance(child, Mapping):
            failures.extend(_check_value(schema, child, value[name], f"{path}.{name}"))
    return failures


def _pattern(template: str) -> re.Pattern[str]:
    return re.compile("^" + re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(template)) + "$")


def test_every_exported_view_carries_exactly_the_keys_the_schema_requires(
    app_method_root: Path, tmp_path: Path
) -> None:
    """The schema calls every property required because the writer never omits one.

    That claim is about `model_dump_json`, not about constructing the model, so it is
    checked against a real export rather than against the models it came from: a field
    with a default that stopped being serialized would leave the UI reading `undefined`
    from a type that says otherwise.
    """

    (app_method_root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    written = export_read_models(app_method_root, tmp_path / "serving")
    schema = build_schema()
    roots = {_pattern(template): reference for template, reference in schema["roots"].items()}  # type: ignore[union-attr]

    checked = 0
    failures: list[str] = []
    for path in written:
        relative = path.relative_to(tmp_path / "serving").as_posix()
        reference = next(
            (target for pattern, target in roots.items() if pattern.match(relative)), None
        )
        if reference is None:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        checked += 1
        failures.extend(_check_value(schema, _definition(schema, reference), payload, relative))

    assert failures == []
    # The fixture publishes a run, a thesis and a ledger, so the export is not one file.
    assert checked > 10


@pytest.mark.parametrize(
    "definition",
    [
        pytest.param({"type": "tuple"}, id="unknown-scalar"),
        pytest.param(
            {
                "type": "object",
                "properties": {"a": {"type": "object", "properties": {"b": {"type": "string"}}}},
                "required": ["a"],
            },
            id="inline-object",
        ),
        pytest.param({"$ref": "https://example.invalid/schema"}, id="external-reference"),
    ],
)
def test_the_renderer_refuses_a_shape_it_does_not_describe(
    definition: dict[str, object],
) -> None:
    """Silence here would emit `unknown` into the UI's types and call it a contract."""

    with pytest.raises(ContractError):
        render_typescript({"$defs": {"Odd": definition}})


@pytest.mark.parametrize(
    "definition",
    [
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
        {"enum": ["one", "two"], "type": "string"},
    ],
)
def test_the_renderer_emits_a_declaration_for_each_supported_definition(
    definition: dict[str, object],
) -> None:
    rendered = render_typescript({"$defs": {"Shape": definition}})

    assert "export type Shape" in rendered or "export interface Shape" in rendered
